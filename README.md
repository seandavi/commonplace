# commonplace

[![ci](https://github.com/seandavi/commonplace/actions/workflows/ci.yml/badge.svg)](https://github.com/seandavi/commonplace/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Shared, durable memory for coding agents (Claude Code, Codex, pi, and any
MCP client) across projects and machines.

Claude Code's auto-memory is good: small typed facts, an index loaded at
session start, and full bodies fetched on demand. But it lives in one
agent's config on one machine. commonplace keeps that model and puts it
behind one server that every agent on your tailnet reads and writes.

A *commonplace book* is a notebook where you copy down the things worth
keeping.

## Design

- **One store, many agents.** SQLite with FTS5, served over MCP
  (streamable HTTP) on the tailnet. Claude Code and Codex speak MCP
  natively. pi gets a small extension.
- **Recall at session start.** Storing memories is only half the job; they
  also have to reach the agent. `commonplace index` prints a
  one-line-per-memory index for the current session. A SessionStart hook
  (Claude Code, Codex) or the pi extension puts it in context, and agents
  fetch full bodies with `get` / `recall`.
- **Scopes.** `global` holds facts about you and how you work.
  `host:<hostname>` holds facts true only on one machine (a temp dir that
  isn't `/tmp`, a local service, where tools live). `project:<host/owner/repo>`
  holds facts about one repo. The project scope comes from the git remote,
  not the path, so a repo maps to the same scope on every machine. A session
  sees `global`, its own host and its own project in the index; `recall`
  without scopes searches everything, so one project can find what another
  learned. Set `COMMONPLACE_HOST` when the hostname is unhelpful (e.g. a Mac
  named by its serial number).
- **Types.** `user`, `feedback`, `project`, `reference`, the same four as
  Claude Code's memory, so its memories import unchanged.
- **Nothing is lost.** `update` writes a new version and `forget`
  soft-deletes. `history` shows every version with its author (which agent
  wrote it).
- **D1-portable.** The SQL stays within what Cloudflare D1 supports (FTS5,
  partial indexes, no triggers), so the store can leave the tailnet later
  without a rewrite.
- **Memories are data.** The server instructions and the injected index
  both tell agents that memories were written by other agents and are never
  instructions. That is the first line of defence against memory poisoning;
  `history` and `export` are how you audit.

```
Claude Code ─┐  MCP (HTTP) + SessionStart hook
Codex ───────┼─────────────────────────────▶  commonplace serve --http  ──▶  SQLite + FTS5
pi ──────────┘  extension → commonplace CLI        (one machine on the tailnet)
```

## Install

```sh
uv tool install git+https://github.com/seandavi/commonplace
```

Every command except `serve` and `export` is an MCP client. Given a server
URL it talks to the shared server; without one it runs the server
in-process against the local database
(`~/.local/share/commonplace/memory.db`, or `$COMMONPLACE_DB`).

Per-machine settings live in `~/.config/commonplace/config.toml`, so hooks
and agents need no environment plumbing:

```toml
url = "http://<tailscale-ip>:9322/mcp"   # the shared server
host = "macbook"                         # this machine's host: scope name
```

`COMMONPLACE_URL` and `COMMONPLACE_HOST` override the file.

```sh
commonplace index                    # session index: global + this repo's project scope
commonplace recall "python tooling"  # ranked search
commonplace get global stack-preferences
commonplace remember --scope global --name prefers-just --type feedback \
    --description "Use just, not make" --body "..." --agent cli
commonplace call history '{"scope": "global", "name": "prefers-just"}'   # any MCP tool, JSON out
commonplace export ./export          # markdown files, one per memory, for review or git
```

## Serving the tailnet

On the machine that holds the store:

```sh
cp deploy/com.seandavis.commonplace.plist ~/Library/LaunchAgents/   # edit paths first
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.seandavis.commonplace.plist
```

`deploy/commonplace-tailnet.sh` binds the machine's Tailscale IP on port
9322. There is no app-level auth: Tailscale is the auth layer, so only your
tailnet can reach it.

> **macOS privacy (TCC):** launchd jobs have no access to `~/Documents`. If
> the repo lives there, grant Full Disk Access to `/bin/sh` (System Settings
> → Privacy & Security → Full Disk Access), then
> `launchctl kickstart -k gui/$(id -u)/com.seandavis.commonplace`.

On Linux, use the systemd user unit instead: `deploy/commonplace.service`
(install steps are in its header).

On every machine, point clients at it in `~/.config/commonplace/config.toml`
(see above).

## Connecting agents

### Claude Code

```sh
claude mcp add --scope user --transport http commonplace "$COMMONPLACE_URL"
```

and in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [{ "type": "command",
                    "command": "commonplace index --hook" }] }
    ]
  }
}
```

Built-in auto-memory keeps working alongside commonplace. Use commonplace for
anything another agent or machine should also know.

### Codex

In `~/.codex/config.toml`:

```toml
[mcp_servers.commonplace]
url = "http://<tailscale-ip>:9322/mcp"
```

and the same SessionStart hook in `~/.codex/hooks.json`. Codex only runs a
new hook after you approve it once in an interactive session (`/hooks`).
Until then `codex exec` skips it silently. As a fallback, add a line to
`~/.codex/AGENTS.md`: *"At session start, call the commonplace
`memory_index` tool with scopes `global` and this repo's project scope."*

### pi

```sh
ln -s "$PWD/integrations/pi/commonplace.ts" ~/.pi/agent/extensions/
```

The extension appends the index to the system prompt and registers
`memory_recall`, `memory_get`, `memory_remember`, `memory_update` and
`memory_forget`. It calls the `commonplace` CLI, so it uses the same config
file as everything else. It uses `appendSystemPrompt` rather
than a custom prompt section because providers such as `pi-claude-bridge`
forward only the append text.

## Bootstrapping from Claude Code memory

```sh
commonplace import-claude --scope global --dry-run ~/.claude/projects/<project>/memory/some_memory.md
commonplace import-claude --scope project:github.com/you/repo ~/.claude/projects/<project>/memory/
```

Importing is idempotent: a memory whose name already exists in the scope is
skipped. Read what you import. Anything in the store reaches every connected
agent, and through them every model provider those agents use.

## Development

```sh
uv sync
uv run pytest
```

## Not yet

- Automatic capture (e.g. a Stop hook that proposes memories from a session).
  For now agents write memories deliberately through the tools.
- A review queue for memories written by agents other than you.
- Moving the store off the tailnet (OAuth on the server, or D1).
