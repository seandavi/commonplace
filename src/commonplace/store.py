"""SQLite store for shared agent memory.

One row per *version* of a memory. A live memory is the row for its
(scope, name) that is neither superseded nor deleted; updates insert a new
row and point the old one at it, so history is never lost. Only live rows
are kept in the FTS index.

The SQL sticks to what Cloudflare D1 also speaks (SQLite + FTS5, partial
indexes, no triggers), so the schema can move there if the store ever needs
to leave the tailnet.
"""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from commonplace.config import setting

TYPES = ("user", "feedback", "project", "reference")
SCOPE_RE = re.compile(r"^(global|host:[a-z0-9][a-z0-9.-]*|project:[a-z0-9][a-z0-9._/-]*)$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")

DEFAULT_MAX_BODY = 4000
# A neighbour is "similar" when its BM25 score against the new memory's name and
# description is at least this fraction of the memory's own score. Calibrated on
# the live store: known duplicates score 0.47-0.61, and 0.45 flags about 18% of memories.
SIMILAR_RATIO = 0.45

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id            TEXT PRIMARY KEY,
    scope         TEXT NOT NULL,
    name          TEXT NOT NULL,
    type          TEXT NOT NULL CHECK (type IN ('user', 'feedback', 'project', 'reference')),
    description   TEXT NOT NULL,
    body          TEXT NOT NULL,
    author        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    superseded_by TEXT,
    deleted_at    TEXT,
    deleted_by    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS memories_live
    ON memories (scope, name)
    WHERE superseded_by IS NULL AND deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS memories_history ON memories (scope, name, created_at);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    id UNINDEXED, scope UNINDEXED, name, description, body,
    tokenize = 'porter unicode61'
);
"""

LIVE = "superseded_by IS NULL AND deleted_at IS NULL"
COLUMNS = "id, scope, name, type, description, body, author, created_at"


class StoreError(ValueError):
    """A request the store refuses: bad input, missing or duplicate memory."""


@dataclass(frozen=True)
class Memory:
    id: str
    scope: str
    name: str
    type: str
    description: str
    body: str
    author: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_db_path() -> Path:
    if env := os.environ.get("COMMONPLACE_DB"):
        return Path(env).expanduser()
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "commonplace" / "memory.db"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def check_scope(scope: str) -> str:
    if not SCOPE_RE.match(scope):
        raise StoreError(
            f"invalid scope {scope!r}: use 'global', 'host:<hostname>' or 'project:<host/owner/repo>'"
        )
    return scope


def _check(name: str, type: str | None = None) -> None:
    if not NAME_RE.match(name):
        raise StoreError(f"invalid name {name!r}: lowercase kebab-case, at most 80 chars")
    if type is not None and type not in TYPES:
        raise StoreError(f"invalid type {type!r}: one of {', '.join(TYPES)}")


def fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 query: quoted terms, OR'd together.

    Quoting every token means user punctuation (colons, hyphens, quotes)
    can never be parsed as FTS5 syntax.
    """
    terms = re.findall(r"\w+", text.lower())
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(terms))


def max_body_chars() -> int:
    """Longest allowed memory body: `max_body` setting, else DEFAULT_MAX_BODY."""
    raw = setting("max_body")
    if raw is None:
        return DEFAULT_MAX_BODY
    try:
        limit = int(raw)
    except ValueError:
        limit = 0
    if limit <= 0:
        raise ValueError(f"max_body must be a positive integer, got {raw!r}")
    return limit


def _check_body(body: str) -> None:
    n, limit = len(body.strip()), max_body_chars()
    if n > limit:
        raise StoreError(
            f"body is {n} characters; the limit is {limit}. "
            "Shorten it, or point to a file, URL or issue that holds the details."
        )


class Store:
    """Async access to the memory database. Use as an async context manager."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else default_db_path()
        self._db: aiosqlite.Connection | None = None
        # All writes share one connection: a commit from one coroutine would
        # otherwise commit another's half-done transaction.
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> Store:
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def open(self) -> None:
        if self._db is not None:
            return
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("store is not open")
        return self._db

    async def _live(self, scope: str, name: str) -> Memory | None:
        cur = await self.db.execute(
            f"SELECT {COLUMNS} FROM memories WHERE scope = ? AND name = ? AND {LIVE}",
            (scope, name),
        )
        row = await cur.fetchone()
        return Memory(**dict(row)) if row else None

    async def _insert(self, m: Memory) -> None:
        await self.db.execute(
            f"INSERT INTO memories ({COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (m.id, m.scope, m.name, m.type, m.description, m.body, m.author, m.created_at),
        )
        await self.db.execute(
            "INSERT INTO memories_fts (id, scope, name, description, body) VALUES (?, ?, ?, ?, ?)",
            (m.id, m.scope, m.name.replace("-", " "), m.description, m.body),
        )

    async def remember(
        self, *, scope: str, name: str, type: str, description: str, body: str, author: str
    ) -> Memory:
        """Create a new memory. Fails if a live memory already has this name in scope."""
        check_scope(scope)
        _check(name, type)
        if not description.strip() or not author.strip():
            raise StoreError("description and author are required")
        _check_body(body)
        async with self._lock:
            if await self._live(scope, name):
                raise StoreError(f"{scope}/{name} already exists; use update to change it")
            m = Memory(uuid.uuid4().hex, scope, name, type, description.strip(), body.strip(), author, _now())
            try:
                await self._insert(m)
                await self.db.commit()
            except sqlite3.IntegrityError as e:
                await self.db.rollback()
                raise StoreError(f"{scope}/{name} already exists; use update to change it") from e
        return m

    async def update(
        self,
        *,
        scope: str,
        name: str,
        author: str,
        description: str | None = None,
        body: str | None = None,
        type: str | None = None,
    ) -> Memory:
        """Supersede a live memory with a new version; unspecified fields carry over."""
        check_scope(scope)
        _check(name, type)
        if body is not None:  # carried-over bodies stay updatable even if oversized
            _check_body(body)
        async with self._lock:
            old = await self._live(scope, name)
            if old is None:
                raise StoreError(f"no live memory {scope}/{name}")
            new = Memory(
                uuid.uuid4().hex,
                scope,
                name,
                type or old.type,
                (description or old.description).strip(),
                (body if body is not None else old.body).strip(),
                author,
                _now(),
            )
            try:
                await self.db.execute("UPDATE memories SET superseded_by = ? WHERE id = ?", (new.id, old.id))
                await self.db.execute("DELETE FROM memories_fts WHERE id = ?", (old.id,))
                await self._insert(new)
                await self.db.commit()
            except sqlite3.IntegrityError as e:
                await self.db.rollback()
                raise StoreError(f"{scope}/{name} changed during the update; get it again and retry") from e
        return new

    async def forget(self, *, scope: str, name: str, author: str) -> Memory:
        """Soft-delete a live memory. It stays in history."""
        check_scope(scope)
        async with self._lock:
            old = await self._live(scope, name)
            if old is None:
                raise StoreError(f"no live memory {scope}/{name}")
            await self.db.execute(
                "UPDATE memories SET deleted_at = ?, deleted_by = ? WHERE id = ?", (_now(), author, old.id)
            )
            await self.db.execute("DELETE FROM memories_fts WHERE id = ?", (old.id,))
            await self.db.commit()
        return old

    async def get(self, *, scope: str, name: str) -> Memory | None:
        check_scope(scope)
        return await self._live(scope, name)

    async def recall(
        self,
        query: str,
        *,
        scopes: list[str] | None = None,
        type: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """BM25-ranked search over live memories; name and description weigh more than body."""
        match = fts_query(query)
        if not match:
            return []
        sql = (
            f"SELECT m.id, m.scope, m.name, m.type, m.description, m.body, m.author, m.created_at, "
            f"bm25(memories_fts, 0, 0, 5.0, 3.0, 1.0) AS score "
            f"FROM memories_fts JOIN memories m ON m.id = memories_fts.id "
            f"WHERE memories_fts MATCH ?"
        )
        params: list[Any] = [match]
        if scopes:
            for s in scopes:
                check_scope(s)
            sql += f" AND m.scope IN ({', '.join('?' * len(scopes))})"
            params += scopes
        if type:
            _check("x", type)
            sql += " AND m.type = ?"
            params.append(type)
        sql += " ORDER BY score LIMIT ?"
        params.append(limit)
        cur = await self.db.execute(sql, params)
        return [dict(r) for r in await cur.fetchall()]

    async def index(self, scopes: list[str] | None = None) -> list[Memory]:
        """All live memories in the given scopes (all scopes if None), for session-start context."""
        sql = f"SELECT {COLUMNS} FROM memories WHERE {LIVE}"
        params: list[Any] = []
        if scopes:
            for s in scopes:
                check_scope(s)
            sql += f" AND scope IN ({', '.join('?' * len(scopes))})"
            params += scopes
        sql += " ORDER BY scope, type, name"
        cur = await self.db.execute(sql, params)
        return [Memory(**dict(r)) for r in await cur.fetchall()]

    async def history(self, *, scope: str, name: str) -> list[dict[str, Any]]:
        """Every version of a memory, oldest first, including superseded and deleted ones."""
        check_scope(scope)
        cur = await self.db.execute(
            f"SELECT {COLUMNS}, superseded_by, deleted_at, deleted_by FROM memories "
            "WHERE scope = ? AND name = ? ORDER BY created_at, rowid",
            (scope, name),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def scopes(self) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            f"SELECT scope, COUNT(*) AS count FROM memories WHERE {LIVE} GROUP BY scope ORDER BY scope"
        )
        return [dict(r) for r in await cur.fetchall()]

    async def similar(self, m: Memory, limit: int = 3) -> list[str]:
        """Names of other live memories in m's scope that look like the same fact."""
        q = fts_query(m.name.replace("-", " ") + " " + m.description)
        if not q:
            return []
        cur = await self.db.execute(
            "SELECT m.id, m.name, bm25(memories_fts, 0, 0, 5.0, 3.0, 1.0) AS score "
            "FROM memories_fts JOIN memories m ON m.id = memories_fts.id "
            "WHERE memories_fts MATCH ? AND m.scope = ? "
            "ORDER BY score LIMIT 10",
            (q, m.scope),
        )
        rows = await cur.fetchall()
        self_score = next((r["score"] for r in rows if r["id"] == m.id), None)
        if not self_score:
            return []
        return [r["name"] for r in rows if r["id"] != m.id and r["score"] / self_score >= SIMILAR_RATIO][:limit]

    async def write_warnings(self, m: Memory) -> list[str]:
        """Warnings for the agent that just wrote m: likely duplicates."""
        warnings = []
        if names := await self.similar(m):
            warnings.append(
                f"Similar memories already in {m.scope}: {', '.join(names)}. If one of them covers "
                f"this fact, update it instead and forget {m.scope}/{m.name}."
            )
        return warnings
