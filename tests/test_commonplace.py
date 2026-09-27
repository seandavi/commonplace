from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastmcp import Client

from commonplace import server
from commonplace.cli import main
from commonplace.importers import _decode_by_walking, _encode, claude_project_path, parse_claude_memory, slugify
from commonplace.scope import host_name, normalize_remote, project_scope, session_scopes
from commonplace.store import Store, StoreError, fts_query

G = "global"
P = "project:github.com/seandavi/vault-mcp"


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch):
    """Never read the developer's real ~/.config/commonplace/config.toml."""
    from commonplace import config

    monkeypatch.setenv("COMMONPLACE_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.delenv("COMMONPLACE_URL", raising=False)
    config._file.cache_clear()
    yield
    config._file.cache_clear()


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


async def test_project_scope_from_git(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_HOST", "box")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert await project_scope(tmp_path) is None
    assert await session_scopes(tmp_path) == [G, "host:box"]
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", "git@github.com:o/r.git"], check=True)
    assert await session_scopes(tmp_path) == [G, "host:box", "project:github.com/o/r"]


@pytest.mark.parametrize("raw, name", [("K3R4F5MWPG", "k3r4f5mwpg"), ("My Mac.local", "my-mac.local"), ("", None)])
def test_host_name(monkeypatch, raw, name):
    monkeypatch.setenv("COMMONPLACE_HOST", raw)
    if name is None:  # empty override falls back to the real hostname
        assert host_name()
    else:
        assert host_name() == name


async def test_host_scope_is_a_valid_scope(store: Store):
    await add(store, "tmp-dir", scope="host:onclappc02", description="Use /data/davsean/tmp, not /tmp")
    assert [m.name for m in await store.index(["global", "host:onclappc02"])] == ["tmp-dir"]
    assert await store.index(["global", "host:k3r4f5mwpg"]) == []


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
        assert "**polars** (user) — Prefer polars" in idx
        assert f"`{P}`" in idx and f"## {P}" not in idx  # scope named, empty section omitted
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


def test_cli_call_bridge(mcp_store):
    r = CliRunner()
    args = '{"scope": "global", "name": "n", "type": "user", "description": "d", "body": "b", "agent": "pi"}'
    out = r.invoke(main, ["call", "remember", args])
    assert out.exit_code == 0, out.output
    assert '"author": "pi"' in out.output
    dup = r.invoke(main, ["call", "remember", args])
    assert dup.exit_code == 1 and "already exists" in dup.output
    bad = r.invoke(main, ["call", "get", "[1]"])
    assert bad.exit_code == 2


def test_cli_process_exits(tmp_path: Path):
    """Regression: an unclosed aiosqlite connection kept the CLI process alive forever."""
    env = {**__import__("os").environ, "COMMONPLACE_DB": str(tmp_path / "m.db")}
    args = '{"scope": "global", "name": "n", "type": "user", "description": "d", "body": "b", "agent": "t"}'
    subprocess.run(["uv", "run", "commonplace", "call", "remember", args], env=env, check=True, timeout=30,
                   capture_output=True)


def test_cli_index_hook_json(mcp_store):
    import json

    out = CliRunner().invoke(main, ["index", "--scope", G, "--hook"])
    payload = json.loads(out.output)
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "Shared memory" in payload["hookSpecificOutput"]["additionalContext"]


def test_config_file_and_env_override(tmp_path: Path, monkeypatch):
    from commonplace import config

    cfg = tmp_path / "config.toml"
    cfg.write_text('url = "http://example:9322/mcp"\nhost = "Laptop"\n')
    monkeypatch.setenv("COMMONPLACE_CONFIG", str(cfg))
    monkeypatch.delenv("COMMONPLACE_HOST", raising=False)
    config._file.cache_clear()
    assert config.setting("url") == "http://example:9322/mcp"
    assert host_name() == "laptop"
    monkeypatch.setenv("COMMONPLACE_HOST", "override")
    assert host_name() == "override"


def test_decode_project_dir_by_walking(tmp_path: Path):
    repo = tmp_path / "git" / "next_flow-telemetry"  # '_' and '-' both encode to '-'
    repo.mkdir(parents=True)
    (tmp_path / "git" / "next").mkdir()  # shorter decoy prefix
    assert _decode_by_walking(_encode(str(repo))) == repo
    assert _decode_by_walking(_encode(str(tmp_path / "gone"))) is None


def test_project_path_prefers_transcript_cwd(tmp_path: Path):
    real = tmp_path / "work"
    real.mkdir()
    pdir = tmp_path / "projects" / "-whatever"
    pdir.mkdir(parents=True)
    (pdir / "s.jsonl").write_text('{"type": "x"}\n{"cwd": "%s"}\n' % real)
    assert claude_project_path(pdir) == real


def test_cli_import_infer_scope(mcp_store, tmp_path: Path):
    repo = tmp_path / "code" / "my-repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:me/my-repo.git"], check=True)
    mem = tmp_path / "projects" / _encode(str(repo)) / "memory"
    mem.mkdir(parents=True)
    (mem / "a.md").write_text("---\nname: a\ndescription: first\ntype: project\n---\nbody\n")
    gone = tmp_path / "projects" / "-nowhere-at-all" / "memory"
    gone.mkdir(parents=True)
    (gone / "b.md").write_text("---\nname: b\ndescription: second\ntype: project\n---\nbody\n")
    r = CliRunner()
    out = r.invoke(main, ["import-claude", "--infer-scope", str(mem), str(gone)])
    assert out.exit_code == 0, out.output
    assert "imported project:github.com/me/my-repo/a" in out.output
    assert "unresolved b" in out.output
    both = r.invoke(main, ["import-claude", "--infer-scope", "--scope", "global", str(mem)])
    assert both.exit_code == 2
