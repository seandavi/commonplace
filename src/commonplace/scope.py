"""Derive memory scopes from where an agent is working.

Three kinds of scope, each visible to a session only when it applies:

- `global`: everywhere.
- `host:<name>`: only on that machine (temp dirs, local services, paths).
  The name is $COMMONPLACE_HOST, else the short hostname, lowercased.
- `project:<host/owner/repo>`: only in that repo. It comes from the git
  remote, not the path, so the same repo maps to the same scope on every
  machine.
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
from pathlib import Path

_SCP = re.compile(r"^[\w.-]+@([\w.-]+):(.+)$")  # git@github.com:owner/repo.git


def normalize_remote(url: str) -> str | None:
    """Map a git remote URL to `host/owner/repo`, or None if it isn't one we understand."""
    url = url.strip()
    if m := _SCP.match(url):
        host, path = m.groups()
    elif m := re.match(r"^[a-z+]+://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$", url):
        host, path = m.groups()
    else:
        return None
    path = path.strip("/").removesuffix(".git")
    if not path:
        return None
    return f"{host}/{path}".lower()


async def _git(cwd: Path, *args: str) -> str | None:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(cwd), *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return out.decode().strip() if proc.returncode == 0 else None


async def project_scope(cwd: Path | str = ".") -> str | None:
    """The project scope for a working directory, or None outside a git repo with a remote."""
    cwd = Path(cwd).expanduser()
    if not cwd.is_dir():
        return None
    url = await _git(cwd, "remote", "get-url", "origin")
    if url is None:
        remotes = await _git(cwd, "remote")
        if remotes:
            url = await _git(cwd, "remote", "get-url", remotes.splitlines()[0])
    slug = normalize_remote(url) if url else None
    return f"project:{slug}" if slug else None


def host_name() -> str:
    name = os.environ.get("COMMONPLACE_HOST") or socket.gethostname().split(".")[0]
    return re.sub(r"[^a-z0-9.-]+", "-", name.lower()).strip("-.") or "localhost"


def host_scope() -> str:
    return f"host:{host_name()}"


async def session_scopes(cwd: Path | str = ".") -> list[str]:
    """Scopes an agent session should see: global, this machine, and the current project if any."""
    project = await project_scope(cwd)
    return ["global", host_scope(), *([project] if project else [])]
