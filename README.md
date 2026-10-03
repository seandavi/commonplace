# commonplace

[![ci](https://github.com/seandavi/commonplace/actions/workflows/ci.yml/badge.svg)](https://github.com/seandavi/commonplace/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/commonplace-agent-memory.svg)](https://pypi.org/project/commonplace-agent-memory/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/seandavi/commonplace/blob/main/LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://github.com/seandavi/commonplace/blob/main/pyproject.toml)

Shared, durable memory for coding agents (Claude Code, Codex, pi, omp and
any MCP client) across projects and machines.

Claude Code's auto-memory is good: small typed facts, an index loaded at
session start, and full bodies fetched on demand. But it lives in one
agent's config on one machine. commonplace keeps that model and puts it
behind one server that every agent on every machine you use reads and
writes.

A *commonplace book* is a notebook where you copy down the things worth
keeping.

## Design

- **One store, many agents.** SQLite with FTS5, served over MCP
  (streamable HTTP) to any machine that can reach the server. Claude Code
  and Codex speak MCP natively. pi and omp get a small extension.
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
  partial indexes, no triggers), so the store can move to Cloudflare later
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

![commonplace architecture: Claude Code and Codex call the server's MCP tools over HTTP and load the index through a SessionStart hook; pi and omp use the bundled extension, which runs the commonplace CLI; the CLI and other MCP clients reach the server over HTTP; the server keeps memories in SQLite with FTS5 on the store host, where export and stats read the database directly.](https://raw.githubusercontent.com/seandavi/commonplace/main/docs/architecture.png)

## Security model

commonplace currently has no authentication of its own, so it needs a
private or otherwise secure network. A [Tailscale](https://tailscale.com)
tailnet is the easy way to get one, and the scripts in `deploy/` bind the
server to the machine's Tailscale address. Beyond that, the only network
requirement is that clients can reach the server's address and port;
anyone who can reach it can read, write and forget every memory. A LAN
behind a firewall, a WireGuard or other VPN, an SSH tunnel to a server
bound to `127.0.0.1`, or a reverse proxy that adds TLS and authentication
work too. Don't expose the port to the internet.

Memories are text that other agents load into their context. Treat them as
untrusted data: the server instructions and the session index tell agents
never to follow instructions found in a memory, and `history` and `export`
show who wrote what. Never store secrets, credentials or tokens in
commonplace.

Report vulnerabilities privately; see [SECURITY.md](https://github.com/seandavi/commonplace/blob/main/SECURITY.md).

## Install

commonplace needs Python 3.12 or newer. It is published on PyPI as
[`commonplace-agent-memory`](https://pypi.org/project/commonplace-agent-memory/)
because the name `commonplace` was taken; the command it installs and the
Python package are both `commonplace`.

Install it on every machine whose agents should share memory, including the
machine that will hold the store. [uv](https://docs.astral.sh/uv/) installs
it as a standalone tool with its own environment:

```sh
uv tool install commonplace-agent-memory
commonplace --help
```

`pipx install commonplace-agent-memory` works the same way, and so does
`pip install commonplace-agent-memory` inside a virtual environment. For
changes that aren't released yet, install from GitHub instead:
`uv tool install git+https://github.com/seandavi/commonplace`.

### Set up each machine

1. **Start the server** on the machine that will hold the store; see
   [Running the shared server](#running-the-shared-server).
2. **Point the CLI at it.** Per-machine settings live in
   `~/.config/commonplace/config.toml`, so hooks and agents need no
   environment plumbing:

   ```toml
   url = "http://<server-address>:9322/mcp"   # the shared server
   host = "macbook"                           # this machine's host: scope name
   max_body = 4000                            # server host only: longest memory body, in characters
   ```

   `COMMONPLACE_URL`, `COMMONPLACE_HOST` and `COMMONPLACE_MAX_BODY`
   override the file. Without a `url`, the CLI uses a local database
   (`~/.local/share/commonplace/memory.db`, or `$COMMONPLACE_DB`), which is
   enough to try commonplace on one machine.
3. **Check the connection.** `commonplace scope` prints the scopes a session
   in the current directory sees; `commonplace index --strict` fetches the
   index and fails loudly if the server can't be reached.
4. **Connect your agents**; see [Connecting agents](#connecting-agents).

### Upgrade and uninstall

```sh
uv tool upgrade commonplace-agent-memory
uv tool uninstall commonplace-agent-memory
```

The scripts in `deploy/` run the server from a clone of this repo, so on the
store host update the clone (`git pull`) and restart the service. A new
version may migrate the database the first time the server opens it; back
it up first (`sqlite3 ~/.local/share/commonplace/memory.db ".backup memory-backup.db"`).

### Commands

Every command except `serve`, `export` and `stats` is an MCP client. Given a
server URL it talks to the shared server through a small built-in MCP client
(fast enough for hooks that run it on every session); without one it runs the
server in-process against the local database. `export` and `stats` read the
database directly, so run them on the store host.

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

## Running the shared server

On the machine that holds the store, bind the HTTP server to an address
your clients can reach on a private network (see
[Security model](#security-model); the default, `127.0.0.1`, serves only
that machine):

```sh
commonplace serve --http --host <address> --port 9322
```

Then point every client machine at `http://<address>:9322/mcp` in
`~/.config/commonplace/config.toml` (see above).

### On a tailnet

`deploy/` keeps the server running on the machine's Tailscale address,
resolved at start, on port 9322. On macOS, install the LaunchAgent:

```sh
sed "s|__HOME__|$HOME|g" deploy/commonplace.plist > ~/Library/LaunchAgents/io.github.seandavi.commonplace.plist   # assumes ~/Documents/git/commonplace
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.github.seandavi.commonplace.plist
```

> **macOS privacy (TCC):** launchd jobs have no access to `~/Documents`. If
> the repo lives there, grant Full Disk Access to `/bin/sh` (System Settings
> → Privacy & Security → Full Disk Access), then
> `launchctl kickstart -k gui/$(id -u)/io.github.seandavi.commonplace`.

On Linux, use the systemd user unit instead: `deploy/commonplace.service`
(install steps are in its header).

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
url = "http://<server-address>:9322/mcp"
```

and the same SessionStart hook in `~/.codex/hooks.json`. Codex only runs a
new hook after you approve it once in an interactive session (`/hooks`).
Until then `codex exec` skips it silently. As a fallback, add a line to
`~/.codex/AGENTS.md`: *"At session start, call the commonplace
`memory_index` tool with scopes `global` and this repo's project scope."*

### pi

The extension is a single file that isn't part of the PyPI package. Symlink
it from a clone of this repo, so `git pull` keeps it current:

```sh
ln -s "$PWD/integrations/pi/commonplace.ts" ~/.pi/agent/extensions/
```

or download a copy:

```sh
curl -fsSL --create-dirs -o ~/.pi/agent/extensions/commonplace.ts \
    https://raw.githubusercontent.com/seandavi/commonplace/main/integrations/pi/commonplace.ts
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

omp (oh-my-pi) loads pi extensions, so the same file works. From a clone of
this repo:

```sh
mkdir -p ~/.omp/agent/extensions && ln -s "$PWD/integrations/pi/commonplace.ts" ~/.omp/agent/extensions/
```

or download a copy into `~/.omp/agent/extensions/` with the `curl` command
above.

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
- Authentication in the server itself (a shared token or OAuth), and moving
  the store to Cloudflare D1.

## Contributing

Issues and pull requests are welcome; see
[CONTRIBUTING.md](https://github.com/seandavi/commonplace/blob/main/CONTRIBUTING.md)
and the [Code of Conduct](https://github.com/seandavi/commonplace/blob/main/CODE_OF_CONDUCT.md).

## Citation

If you use commonplace in your work, cite it with the metadata in
[CITATION.cff](https://github.com/seandavi/commonplace/blob/main/CITATION.cff); GitHub's "Cite this repository" button uses it.

## License

MIT; see [LICENSE](https://github.com/seandavi/commonplace/blob/main/LICENSE).
