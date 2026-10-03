# commonplace

[![ci](https://github.com/seandavi/commonplace/actions/workflows/ci.yml/badge.svg)](https://github.com/seandavi/commonplace/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)

Shared, durable memory for coding agents (Claude Code, Codex, pi, omp and
any MCP client) across projects and machines.

Claude Code's auto-memory is good: small typed facts, an index loaded at
session start, and full bodies fetched on demand. But it lives in one
agent's config on one machine. commonplace keeps that model and puts it
behind one server that every agent on your tailnet reads and writes.

A *commonplace book* is a notebook where you copy down the things worth
keeping.

## Design

- **One store, many agents.** SQLite with FTS5, served over MCP
  (streamable HTTP) on the tailnet. Claude Code and Codex speak MCP
  natively. pi and omp get a small extension.
- **Recall at session start.** Storing memories is only half the job; they
  also have to reach the agent. `commonplace index` prints a
  one-line-per-memory index for the current session. A SessionStart hook
  (Claude Code, Codex) or the pi/omp extension puts it in context, and agents
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
- **Write guards.** Memories are short facts, not documents or status logs.
  The server rejects bodies over a size limit (4,000 characters by default;
  `max_body` in config.toml or `COMMONPLACE_MAX_BODY` on the server host).
  `remember` and `update` return `warnings` naming similar memories in the
  scope, and flag a scope whose index is over 8,000 characters.
- **Expiry.** A memory can carry an `expires` date (YYYY-MM-DD); from that
  date it leaves the index and `recall`, while `get` still returns it.
  Project lines in the index show how many days ago they were last updated.
- **Usage.** The server counts tool calls per day and reads per memory;
  `commonplace stats` on the store host shows what gets used and what never
  does.

```
Claude Code ─┐  MCP (HTTP) + SessionStart hook
Codex ───────┼─────────────────────────────▶  commonplace serve --http  ──▶  SQLite + FTS5
pi, omp ─────┘  extension → commonplace CLI        (one machine on the tailnet)
```

## Security model

commonplace has no authentication of its own. Run the HTTP server only on a
network you trust: the deployment scripts in `deploy/` bind it to the
machine's Tailscale address, so only devices on your tailnet can reach it.
Anyone who can reach the port can read, write and forget every memory.

Memories are text that other agents load into their context. Treat them as
untrusted data: the server instructions and the session index tell agents
never to follow instructions found in a memory, and `history` and `export`
show who wrote what. Never store secrets, credentials or tokens in
commonplace.

Report vulnerabilities privately; see [SECURITY.md](SECURITY.md).

## Install

```sh
uv tool install git+https://github.com/seandavi/commonplace
```

Every command except `serve`, `export` and `stats` is an MCP client. Given a
server URL it talks to the shared server through a small built-in MCP client
(fast enough for hooks that run it on every session); without one it runs the
server in-process against the local database
(`~/.local/share/commonplace/memory.db`, or `$COMMONPLACE_DB`). `export` and
`stats` read the database directly, so run them on the store host.

Per-machine settings live in `~/.config/commonplace/config.toml`, so hooks
and agents need no environment plumbing:

```toml
url = "http://<tailscale-ip>:9322/mcp"   # the shared server
host = "macbook"                         # this machine's host: scope name
max_body = 4000                          # server host only: longest memory body, in characters
```

`COMMONPLACE_URL`, `COMMONPLACE_HOST` and `COMMONPLACE_MAX_BODY` override the file.

```sh
commonplace index                    # session index: global + this host + this repo's project scope
commonplace index --instructions     # the server's rules for agents, then the index
commonplace recall "python tooling"  # ranked search
commonplace get global stack-preferences
commonplace remember --scope global --name prefers-just --type feedback \
    --description "Use just, not make" --body "..." --agent cli
commonplace remember --scope project:github.com/you/repo --name freeze --type project \
    --description "Release freeze until the 1.0 tag" --body "..." --expires 2026-12-31
commonplace update global prefers-just --body - <<'EOF'
Use just, not make. Quotes, `backticks` and $VARS pass through stdin untouched.
EOF
commonplace call history '{"scope": "global", "name": "prefers-just"}'   # any MCP tool, JSON out
commonplace export ./export          # markdown files, one per memory, for review or git
commonplace stats --days 30          # tool calls, most-read and never-read memories (store host)
```

## Serving the tailnet

On the machine that holds the store:

```sh
sed "s|__HOME__|$HOME|g" deploy/commonplace.plist > ~/Library/LaunchAgents/io.github.seandavi.commonplace.plist   # assumes ~/Documents/git/commonplace
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.github.seandavi.commonplace.plist
```

`deploy/commonplace-tailnet.sh` binds the machine's Tailscale IP on port
9322. There is no app-level auth: Tailscale is the auth layer, so only your
tailnet can reach it.

> **macOS privacy (TCC):** launchd jobs have no access to `~/Documents`. If
> the repo lives there, grant Full Disk Access to `/bin/sh` (System Settings
> → Privacy & Security → Full Disk Access), then
> `launchctl kickstart -k gui/$(id -u)/io.github.seandavi.commonplace`.

On Linux, use the systemd user unit instead: `deploy/commonplace.service`
(install steps are in its header).

On every machine, point clients at it in `~/.config/commonplace/config.toml`
(see above).

## Connecting agents

There are two ways in: register the server as an MCP server and load the
index with a session-start hook (Claude Code, Codex, other MCP clients), or
load the bundled extension (pi, omp), which does both through the CLI.

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

At session start the extension loads the server's rules for agents and the
index (`commonplace index --instructions`) and appends them to the system
prompt. It registers `memory_recall`, `memory_get`, `memory_remember`,
`memory_update` and `memory_forget`, which call the `commonplace` CLI, so it
uses the same config file as everything else; set `COMMONPLACE_BIN` to use
another CLI binary. It uses `appendSystemPrompt` rather than a custom prompt
section because providers such as `pi-claude-bridge` forward only the append
text.

### omp

omp (oh-my-pi) loads pi extensions, so the same file works:

```sh
mkdir -p ~/.omp/agent/extensions && ln -s "$PWD/integrations/pi/commonplace.ts" ~/.omp/agent/extensions/
```

The rules and the index are added to the system prompt on every turn, and
writes record the author as `omp@<host>`. Don't also list the server in
`~/.omp/agent/mcp.json`, or omp gets two sets of memory tools.

### Other MCP clients

Register the server URL as a streamable-HTTP MCP server; the rules arrive as
MCP server instructions. At session start the agent should call
`memory_index` with the scopes `commonplace scope` prints for the working
directory. Clients with a session-start hook can run `commonplace index`
(add `--instructions` if the client drops server instructions); clients
without one need the one-line instruction shown for Codex above.

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

## Contributing

Issues and pull requests are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md)
and the [Code of Conduct](CODE_OF_CONDUCT.md).

## Citation

If you use commonplace in your work, cite it with the metadata in
[CITATION.cff](CITATION.cff); GitHub's "Cite this repository" button uses it.

## License

MIT; see [LICENSE](LICENSE).
