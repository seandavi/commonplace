from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastmcp import Client

from commonplace import server
from commonplace.cli import main
from commonplace.importers import parse_claude_memory, slugify
from commonplace.scope import normalize_remote, project_scope, session_scopes
from commonplace.store import Store, StoreError, fts_query

G = "global"
P = "project:github.com/seandavi/vault-mcp"


@pytest.fixture
async def store(tmp_path: Path):
    async with Store(tmp_path / "m.db") as s:
        yield s


async def add(store: Store, name: str, scope: str = G, **kw):
    fields = {"type": "feedback", "description": f"about {name}", "body": f"body of {name}", "author": "test"}
    fields.update(kw)
    return await store.remember(scope=scope, name=name, **fields)


# --- store -----------------------------------------------------------------


async def test_remember_and_get(store: Store):
    await add(store, "use-uv", description="Use uv for Python packaging")
    m = await store.get(scope=G, name="use-uv")
    assert m is not None and m.description == "Use uv for Python packaging"
    assert await store.get(scope=P, name="use-uv") is None


async def test_duplicate_name_in_scope_rejected_but_other_scope_ok(store: Store):
    await add(store, "x")
    with pytest.raises(StoreError, match="already exists"):
        await add(store, "x")
    await add(store, "x", scope=P)


@pytest.mark.parametrize(
    "kw",
    [
        {"scope": "Global"},
        {"scope": "project:"},
        {"name": "Not Kebab"},
        {"type": "opinion"},
        {"description": "  "},
    ],
)
async def test_validation(store: Store, kw):
    args = {"scope": G, "name": "ok", "type": "user", "description": "d", "body": "b", "author": "t"} | kw
    with pytest.raises(StoreError):
        await store.remember(**args)


async def test_update_keeps_history_and_reindexes(store: Store):
    await add(store, "stack", body="pandas everywhere")
    await store.update(scope=G, name="stack", body="polars, not pandas", author="codex")
    live = await store.get(scope=G, name="stack")
    assert live.body == "polars, not pandas" and live.author == "codex"
    assert live.description == "about stack"  # carried over
    hist = await store.history(scope=G, name="stack")
    assert [h["body"] for h in hist] == ["pandas everywhere", "polars, not pandas"]
    assert hist[0]["superseded_by"] == hist[1]["id"]
    assert [h["name"] for h in await store.recall("pandas")] == ["stack"]  # one live hit, not two
    assert await store.recall("everywhere") == []  # old body no longer searchable


async def test_forget_is_soft(store: Store):
    await add(store, "gone")
    await store.forget(scope=G, name="gone", author="pi")
    assert await store.get(scope=G, name="gone") is None
    assert await store.recall("gone") == []
    hist = await store.history(scope=G, name="gone")
    assert hist[0]["deleted_by"] == "pi"
    await add(store, "gone")  # name is free again


async def test_recall_ranks_name_and_description_over_body(store: Store):
    await add(store, "misc", body="we once mentioned duckdb in passing among many other words here")
    await add(store, "duckdb-fts", description="DuckDB full-text search limits")
    hits = await store.recall("duckdb")
    assert [h["name"] for h in hits] == ["duckdb-fts", "misc"]


async def test_recall_filters(store: Store):
    await add(store, "a", body="tailnet")
    await add(store, "b", scope=P, body="tailnet", type="project")
    assert {h["name"] for h in await store.recall("tailnet", scopes=[P])} == {"b"}
    assert {h["name"] for h in await store.recall("tailnet", type="feedback")} == {"a"}


@pytest.mark.parametrize("q", ['"unbalanced', "a:b OR", "NEAR(x", "-*", "project:github.com/x"])
async def test_recall_survives_fts_syntax(store: Store, q: str):
    await add(store, "x")
    await store.recall(q)  # must not raise


def test_fts_query():
    assert fts_query("Use uv, use UV!") == '"use" OR "uv"'
    assert fts_query("!!!") == ""


async def test_index_and_scopes(store: Store):
    await add(store, "b")
    await add(store, "a", scope=P)
    assert [m.name for m in await store.index([G])] == ["b"]
    assert [m.name for m in await store.index()] == ["b", "a"]
    assert await store.scopes() == [{"scope": G, "count": 1}, {"scope": P, "count": 1}]


# --- scope -----------------------------------------------------------------


@pytest.mark.parametrize(
    "url, slug",
    [
        ("git@github.com:seandavi/vault-mcp.git", "github.com/seandavi/vault-mcp"),
        ("https://github.com/SeanDavi/Vault-MCP", "github.com/seandavi/vault-mcp"),
        ("https://user:tok@gitlab.example.org:8443/grp/sub/proj.git/", "gitlab.example.org/grp/sub/proj"),
        ("ssh://git@github.com/o/r.git", "github.com/o/r"),
        ("/some/local/path", None),
    ],
)
def test_normalize_remote(url, slug):
    assert normalize_remote(url) == slug


async def test_project_scope_from_git(tmp_path: Path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert await project_scope(tmp_path) is None
    assert await session_scopes(tmp_path) == [G]
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", "git@github.com:o/r.git"], check=True)
    assert await session_scopes(tmp_path) == [G, "project:github.com/o/r"]


# --- importer --------------------------------------------------------------


def test_parse_claude_memory(tmp_path: Path):
    f = tmp_path / "user_role.md"
    f.write_text("---\nname: user_role\ndescription: Who the user is\nmetadata:\n  type: user\n---\n\nA scientist.\n")
    m = parse_claude_memory(f)
    assert (m.name, m.type, m.description, m.body) == ("user-role", "user", "Who the user is", "A scientist.")
    (tmp_path / "MEMORY.md").write_text("- index")
    assert parse_claude_memory(tmp_path / "MEMORY.md") is None
    (tmp_path / "plain.md").write_text("no frontmatter")
    assert parse_claude_memory(tmp_path / "plain.md") is None


def test_slugify():
    assert slugify("Feedback_PR workflow!") == "feedback-pr-workflow"


# --- MCP + CLI -------------------------------------------------------------


@pytest.fixture
async def mcp_store(tmp_path: Path, monkeypatch):
    s = Store(tmp_path / "m.db")
    server.set_store(s)
    monkeypatch.setenv("COMMONPLACE_DB", str(tmp_path / "m.db"))
    yield s
    await s.close()
    server.set_store(None)


async def test_mcp_round_trip(mcp_store):
    async with Client(server.mcp) as c:
        await c.call_tool(
            "remember",
            {"scope": G, "name": "polars", "type": "user", "description": "Prefer polars", "body": "b", "agent": "t"},
        )
        dup = await c.call_tool(
            "remember",
            {"scope": G, "name": "polars", "type": "user", "description": "d", "body": "b", "agent": "t"},
            raise_on_error=False,
        )
        assert dup.is_error and "already exists" in dup.content[0].text
        idx = (await c.call_tool("memory_index", {"scopes": [G, P]})).data
        assert "**polars** (user) — Prefer polars" in idx and f"## {P}\n\n(none yet)" in idx
        hits = (await c.call_tool("recall", {"query": "polars"})).data
        assert hits[0]["name"] == "polars"


def test_cli_import_and_index(mcp_store, tmp_path: Path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "a.md").write_text("---\nname: a\ndescription: first\ntype: feedback\n---\nbody a\n")
    (mem / "MEMORY.md").write_text("- [a](a.md)")
    r = CliRunner()
    out = r.invoke(main, ["import-claude", str(mem), "--scope", G])
    assert out.exit_code == 0, out.output
    assert "imported global/a" in out.output
    again = r.invoke(main, ["import-claude", str(mem), "--scope", G])
    assert "skip global/a (exists)" in again.output
    idx = r.invoke(main, ["index", "--scope", G])
    assert "**a** (feedback) — first" in idx.output


def test_cli_index_unreachable_server_is_silent():
    r = CliRunner()
    out = r.invoke(main, ["--url", "http://127.0.0.1:9/mcp", "index", "--scope", G])
    assert out.exit_code == 0
    assert "Shared memory" not in out.output
