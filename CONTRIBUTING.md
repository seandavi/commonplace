# Contributing to commonplace

Thanks for your interest! Issues and pull requests are welcome.

## Development setup

Requirements: Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest                      # store, MCP, CLI and a real HTTP server on 127.0.0.1
COMMONPLACE_DB=/tmp/scratch.db uv run commonplace serve --http   # local server on 127.0.0.1:9322
```

## Design norms

Read these before proposing structural changes:

- **Writes are deliberate.** Agents write memories through tools. No transcript capture, no LLM on the server, no background workers.
- **Nothing is lost.** `update` adds a version and `forget` soft-deletes. Don't add destructive operations.
- **D1-portable SQL.** Stay within what Cloudflare D1 supports: SQLite with FTS5, partial indexes and upserts; no triggers.
- **Memories are data.** Memory text must never reach an agent as instructions.
- **Fast CLI.** `commonplace.cli` must not import fastmcp at module level (a test enforces it); hooks and extensions start the CLI on every call.

## Pull requests

- CI (`uv run pytest`) must be green.
- Add or update tests for every behavior change.
- MCP tool names and arguments reach every connected agent; keep them backward compatible or explain why in the PR.
- Keep `version` in `pyproject.toml` and `CITATION.cff` in sync.
- `docs/architecture.png` is rendered from `docs/architecture.archify.json` with [archify](https://github.com/tt-a1i/archify): render the JSON to HTML, open it, and use Export → PNG in the light theme. Change both together.

## Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
