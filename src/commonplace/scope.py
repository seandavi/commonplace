"""Derive memory scopes from where an agent is working.

A project's scope comes from its git remote, not its path, so the same repo
maps to the same scope on every machine: `project:github.com/owner/repo`.
"""

from __future__ import annotations

import asyncio
import re
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


async def session_scopes(cwd: Path | str = ".") -> list[str]:
    """Scopes an agent session should see: global, plus the current project's if any."""
    project = await project_scope(cwd)
    return ["global", project] if project else ["global"]
