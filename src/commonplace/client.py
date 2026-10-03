"""How the CLI reaches the memory tools.

Remote calls use a small MCP client built on the standard library, because
importing fastmcp costs about 0.7 s and session hooks and the pi/omp extension
start the CLI once per call. Local calls (no server URL) run the server
in-process through fastmcp, imported only then.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from importlib.metadata import version
from typing import Any, Protocol

PROTOCOL_VERSION = "2025-06-18"
TIMEOUT_SECONDS = 5
DISTRIBUTION = "commonplace-agent-memory"  # the PyPI name; `commonplace` is taken


class ToolCallError(Exception):
    """The server refused the call: bad input, missing or duplicate memory."""


class RemoteError(Exception):
    """The server was unreachable, or answered with something other than MCP."""


class Connection(Protocol):
    instructions: str

    async def call(self, tool: str, args: dict[str, Any]) -> Any: ...


def _drop_none(args: dict[str, Any]) -> dict[str, Any]:
    """Omitted arguments are None; "" is a real value (e.g. clearing `expires`)."""
    return {k: v for k, v in args.items() if v is not None}


def _find_response(content_type: str, body: str, request_id: int) -> dict[str, Any]:
    """The JSON-RPC result for request_id from a JSON or SSE response body."""
    try:
        if "text/event-stream" in content_type:
            messages = [json.loads(line[5:]) for line in body.splitlines() if line.startswith("data:")]
        else:
            messages = [json.loads(body)] if body.strip() else []
    except json.JSONDecodeError as e:
        raise RemoteError(f"not an MCP response: {e}") from e
    for msg in messages:
        if isinstance(msg, dict) and msg.get("id") == request_id:
            if "error" in msg:
                raise RemoteError(msg["error"].get("message", "unknown error"))
            return msg.get("result") or {}
    raise RemoteError(f"no response to request {request_id}")


class _HttpConnection:
    """MCP over streamable HTTP: initialize, tools/call, DELETE to end the session."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.instructions = ""
        self._session: str | None = None
        self._protocol: str | None = None
        self._next_id = 1

    def _send(self, method: str, payload: dict[str, Any] | None = None) -> tuple[Mapping[str, str], str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self._session:
            headers["mcp-session-id"] = self._session
        if self._protocol:
            headers["mcp-protocol-version"] = self._protocol
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                return resp.headers, resp.read().decode()
        except urllib.error.HTTPError as e:
            raise RemoteError(f"{self.url}: HTTP {e.code}") from e
        except (urllib.error.URLError, OSError) as e:  # timeouts are OSErrors too
            raise RemoteError(f"cannot reach {self.url}: {getattr(e, 'reason', e)}") from e

    def _rpc(self, method: str, params: dict[str, Any]) -> tuple[Mapping[str, str], dict[str, Any]]:
        request_id = self._next_id
        self._next_id += 1
        headers, body = self._send("POST", {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return headers, _find_response(headers.get("Content-Type", ""), body, request_id)

    def open(self) -> None:
        headers, result = self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "commonplace-cli", "version": version(DISTRIBUTION)},
            },
        )
        self._session = headers.get("mcp-session-id")
        self._protocol = result.get("protocolVersion") or PROTOCOL_VERSION
        self.instructions = result.get("instructions") or ""
        self._send("POST", {"jsonrpc": "2.0", "method": "notifications/initialized"})  # 202, empty body

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        # Blocking I/O is fine here: the CLI process makes one call at a time.
        _, result = self._rpc("tools/call", {"name": tool, "arguments": _drop_none(args)})
        content = result.get("content") or []
        if result.get("isError"):
            raise ToolCallError(content[0].get("text", "tool call failed") if content else "tool call failed")
        structured = result.get("structuredContent")
        if structured is not None:
            wrapped = ((result.get("_meta") or {}).get("fastmcp") or {}).get("wrap_result")
            return structured["result"] if wrapped else structured
        return "\n".join(c.get("text", "") for c in content if c.get("type") == "text")

    def close(self) -> None:
        if self._session is None:
            return
        try:
            self._send("DELETE")
        except Exception:  # the session expires on its own; never fail a finished command
            pass


class _LocalConnection:
    """The server's tools in-process, through fastmcp's client."""

    def __init__(self, client: Any, instructions: str) -> None:
        self._client = client
        self.instructions = instructions

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        from fastmcp.exceptions import ToolError

        try:
            return (await self._client.call_tool(tool, _drop_none(args))).data
        except ToolError as e:
            raise ToolCallError(str(e)) from e


@asynccontextmanager
async def connect(url: str | None) -> AsyncIterator[Connection]:
    """A connection to the shared server at `url`, or to the local database if None."""
    if url:
        conn = _HttpConnection(url)
        conn.open()
        try:
            yield conn
        finally:
            conn.close()
        return

    from fastmcp import Client

    from commonplace import server

    try:
        async with Client(server.mcp) as client:
            yield _LocalConnection(client, server.INSTRUCTIONS)
    finally:
        # aiosqlite runs a non-daemon thread; an open connection would keep
        # the process alive after the command finishes.
        await server.close_store()
