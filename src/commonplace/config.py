"""Per-machine defaults: ~/.config/commonplace/config.toml.

    url = "http://<tailscale-ip>:9322/mcp"   # shared server; omit to use the local DB
    host = "my-laptop"                       # name for this machine's host: scope
    max_body = 4000   # server host only: longest memory body, in characters

Environment variables (COMMONPLACE_URL, COMMONPLACE_HOST) override the file,
so agents and hooks need no environment plumbing of their own.
"""

from __future__ import annotations

import os
import tomllib
from functools import cache
from pathlib import Path


def config_path() -> Path:
    if env := os.environ.get("COMMONPLACE_CONFIG"):
        return Path(env).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "commonplace" / "config.toml"


@cache
def _file() -> dict[str, str]:
    try:
        data = tomllib.loads(config_path().read_text())
    except FileNotFoundError:
        return {}
    return {k: str(v) for k, v in data.items() if isinstance(v, (str, int)) and not isinstance(v, bool)}


def setting(key: str) -> str | None:
    """COMMONPLACE_<KEY> from the environment, else `key` from the config file."""
    return os.environ.get(f"COMMONPLACE_{key.upper()}") or _file().get(key) or None
