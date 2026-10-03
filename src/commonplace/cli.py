"""`commonplace` command line: run the server, and read/write memory from hooks and scripts.

Every read/write command is an MCP client. With COMMONPLACE_URL (or --url)
set it talks to the shared server over HTTP; otherwise it runs the server
in-process against the local database. One code path either way.
"""

from __future__ import annotations

import asyncio
import json
import sys
from functools import wraps
from pathlib import Path
from typing import Any

import click
from fastmcp import Client
from fastmcp.exceptions import ToolError

from commonplace.config import setting
from commonplace.importers import claude_memory_files, claude_project_path, parse_claude_memory
from commonplace.scope import host_name, project_scope, session_scopes
from commonplace.server import close_store, mcp
from commonplace.store import TYPES, Store, default_db_path, max_body_chars


def _client(ctx: click.Context) -> Client:
    url = ctx.obj.get("url")
    return Client(url) if url else Client(mcp)


async def _call(ctx: click.Context, tool: str, **args: Any) -> Any:
    async with _client(ctx) as client:
        result = await client.call_tool(tool, {k: v for k, v in args.items() if v is not None})
        return result.data


def run_async(f: Any) -> Any:
    @wraps(f)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        async def main() -> Any:
            try:
                return await f(*args, **kwargs)
            finally:
                # aiosqlite runs a non-daemon thread; an open connection
                # would keep the process alive after the command finishes.
                await close_store()

        try:
            return asyncio.run(main())
        except ToolError as e:
            raise click.ClickException(str(e)) from e

    return wrapper


def _echo_json(data: Any) -> None:
    click.echo(json.dumps(data, indent=2, default=str))


@click.group()
@click.option("--url", help="Shared server, e.g. http://host:9322/mcp. Default: $COMMONPLACE_URL, the config file, else the local DB.")
@click.pass_context
def main(ctx: click.Context, url: str | None) -> None:
    """Shared, durable memory for coding agents.

    Per-machine defaults (server url, host name) live in
    ~/.config/commonplace/config.toml.
    """
    ctx.obj = {"url": url or setting("url")}


@main.command()
@click.option("--http", "use_http", is_flag=True, help="Serve streamable HTTP instead of stdio.")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=9322, show_default=True)
def serve(use_http: bool, host: str, port: int) -> None:
    """Run the MCP server against the local database."""
    try:
        max_body_chars()
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    if use_http:
        mcp.run(transport="http", host=host, port=port, show_banner=False)
    else:
        mcp.run(show_banner=False)


@main.command()
@click.option("--scope", "scopes", multiple=True, help="Scope to include (repeatable). Default: global + this project.")
@click.option("--cwd", default=".", type=click.Path(file_okay=False), help="Directory to derive the project scope from.")
@click.option("--strict", is_flag=True, help="Fail loudly if the server is unreachable (default: print nothing).")
@click.option("--hook", "as_hook", is_flag=True, help="Emit SessionStart hook JSON (Claude Code, Codex) instead of markdown.")
@click.pass_context
@run_async
async def index(ctx: click.Context, scopes: tuple[str, ...], cwd: str, strict: bool, as_hook: bool) -> None:
    """Print the memory index for a session. Built for session-start hooks.

    Unless --strict, an unreachable server prints nothing and exits 0, so a
    hook never breaks an agent's startup.
    """
    wanted = list(scopes) or await session_scopes(cwd)
    try:
        text = await _call(ctx, "memory_index", scopes=wanted)
    except Exception as e:
        if strict:
            raise
        click.echo(f"commonplace: index unavailable ({e.__class__.__name__}: {e})", err=True)
        return
    if as_hook:
        payload = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
        click.echo(json.dumps(payload))
    else:
        click.echo(text, nl=False)


@main.command()
@click.argument("cwd", default=".", type=click.Path(file_okay=False))
@run_async
async def scope(cwd: str) -> None:
    """Print the scopes a session in CWD sees."""
    for s in await session_scopes(cwd):
        click.echo(s)


@main.command()
@click.argument("tool")
@click.argument("args_json", default="{}")
@click.pass_context
@run_async
async def call(ctx: click.Context, tool: str, args_json: str) -> None:
    """Call any MCP tool with a JSON object of arguments; print the JSON result.

    The bridge for agents that can run commands but don't speak MCP
    (e.g. the pi extension): same tools, same semantics.
    """
    try:
        args = json.loads(args_json)
    except json.JSONDecodeError as e:
        raise click.BadParameter(f"not JSON: {e}", param_hint="ARGS_JSON") from e
    if not isinstance(args, dict):
        raise click.BadParameter("must be a JSON object", param_hint="ARGS_JSON")
    _echo_json(await _call(ctx, tool, **args))


@main.command()
@click.argument("query")
@click.option("--scope", "scopes", multiple=True)
@click.option("--type", "type_", type=click.Choice(TYPES))
@click.option("--limit", default=10, show_default=True)
@click.pass_context
@run_async
async def recall(ctx: click.Context, query: str, scopes: tuple[str, ...], type_: str | None, limit: int) -> None:
    """Search memories."""
    hits = await _call(ctx, "recall", query=query, scopes=list(scopes) or None, type=type_, limit=limit)
    for h in hits:
        click.echo(f"{h['scope']}/{h['name']} ({h['type']}) — {h['description']}")


@main.command()
@click.argument("scope_")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
@run_async
async def get(ctx: click.Context, scope_: str, name: str, as_json: bool) -> None:
    """Show one memory."""
    m = await _call(ctx, "get", scope=scope_, name=name)
    if as_json:
        _echo_json(m)
    else:
        click.echo(f"# {m['name']} ({m['type']}, {m['scope']})\n{m['description']}\n\n{m['body']}")


@main.command()
@click.option("--scope", "scope_", required=True)
@click.option("--name", required=True)
@click.option("--type", "type_", required=True, type=click.Choice(TYPES))
@click.option("--description", required=True)
@click.option("--body", default="-", help="Body text, or - to read stdin.")
@click.option("--agent", default=None, help="Author to record. Default: cli@<host>.")
@click.pass_context
@run_async
async def remember(
    ctx: click.Context, scope_: str, name: str, type_: str, description: str, body: str, agent: str
) -> None:
    """Save a new memory."""
    if body == "-":
        body = sys.stdin.read()
    agent = agent or f"cli@{host_name()}"
    m = await _call(
        ctx, "remember", scope=scope_, name=name, type=type_, description=description, body=body, agent=agent
    )
    click.echo(f"remembered {m['scope']}/{m['name']}")
    for w in m.get("warnings", []):
        click.echo(f"warning: {w}", err=True)


@main.command()
@click.argument("scope_")
@click.argument("name")
@click.option("--agent", default=None, help="Author to record. Default: cli@<host>.")
@click.pass_context
@run_async
async def forget(ctx: click.Context, scope_: str, name: str, agent: str) -> None:
    """Retire a memory (kept in history)."""
    agent = agent or f"cli@{host_name()}"
    await _call(ctx, "forget", scope=scope_, name=name, agent=agent)
    click.echo(f"forgot {scope_}/{name}")


@main.command(name="import-claude")
@click.argument("paths", nargs=-1, required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--scope", "scope_", help="Scope for every imported memory.")
@click.option("--infer-scope", is_flag=True,
              help="Scope each memory by its Claude project's git remote (project:<host/owner/repo>).")
@click.option("--agent", default=None, help="Author to record. Default: claude-code@<host>.")
@click.option("--dry-run", is_flag=True)
@click.pass_context
@run_async
async def import_claude(
    ctx: click.Context, paths: tuple[Path, ...], scope_: str | None, infer_scope: bool, agent: str | None, dry_run: bool
) -> None:
    """Import Claude Code memory files (or memory/ directories).

    Give --scope for a fixed scope, or --infer-scope to put each memory in
    the project scope of the repo its Claude project directory belongs to.
    Memories whose project can't be resolved (directory gone, no git remote)
    are reported and skipped. Existing memories with the same name are left
    alone, so re-running is safe.
    """
    if bool(scope_) == infer_scope:
        raise click.UsageError("give exactly one of --scope or --infer-scope")
    agent = agent or f"claude-code@{host_name()}"
    scopes: dict[Path, str | None] = {}

    async def scope_for(f: Path) -> str | None:
        if scope_:
            return scope_
        project_dir = f.resolve().parent.parent
        if project_dir not in scopes:
            path = claude_project_path(project_dir)
            scopes[project_dir] = await project_scope(path) if path else None
        return scopes[project_dir]

    async with _client(ctx) as client:
        for f in claude_memory_files(list(paths)):
            m = parse_claude_memory(f)
            if m is None:
                continue
            scope = await scope_for(f)
            if scope is None:
                click.echo(f"unresolved {m.name} ← {f} (project dir gone or no git remote)")
                continue
            if dry_run:
                click.echo(f"would import {scope}/{m.name} ({m.type}) — {m.description[:90]} ← {f}")
                if m.warning:
                    click.echo(f"  warning: {m.warning}")
                continue
            existing = await client.call_tool("get", {"scope": scope, "name": m.name}, raise_on_error=False)
            if not existing.is_error:
                click.echo(f"skip {scope}/{m.name} (exists)")
                continue
            await client.call_tool(
                "remember",
                {"scope": scope, "name": m.name, "type": m.type, "description": m.description,
                 "body": m.body, "agent": agent},
            )
            click.echo(f"imported {scope}/{m.name}")
            if m.warning:
                click.echo(f"  warning: {m.warning}")


@main.command()
@click.argument("out_dir", type=click.Path(file_okay=False, path_type=Path))
@click.option("--db", type=click.Path(dir_okay=False, path_type=Path), help="Default: $COMMONPLACE_DB or ~/.local/share/commonplace/memory.db")
@run_async
async def export(out_dir: Path, db: Path | None) -> None:
    """Write live memories as markdown files (one per memory) for review or git.

    Reads the database directly, so run it on the machine that hosts the store.
    """
    async with Store(db or default_db_path()) as store:
        memories = await store.index()
    for m in memories:
        folder = out_dir / m.scope.replace(":", "/")
        folder.mkdir(parents=True, exist_ok=True)
        front = "\n".join(
            f"{k}: {json.dumps(v)}"
            for k, v in [("name", m.name), ("description", m.description), ("type", m.type),
                         ("scope", m.scope), ("author", m.author), ("updated", m.created_at)]
        )
        (folder / f"{m.name}.md").write_text(f"---\n{front}\n---\n\n{m.body}\n")
    click.echo(f"exported {len(memories)} memories to {out_dir}")

