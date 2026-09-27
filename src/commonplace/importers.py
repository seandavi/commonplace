"""Import memories written by other systems (currently Claude Code auto-memory)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from commonplace.store import TYPES


@dataclass(frozen=True)
class ImportedMemory:
    name: str
    type: str
    description: str
    body: str
    source: Path


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:80].rstrip("-") or "memory"


def parse_claude_memory(path: Path) -> ImportedMemory | None:
    """Parse one Claude Code memory file (YAML frontmatter + markdown body).

    Returns None for files that aren't memories (the MEMORY.md index, files
    without frontmatter, or an unknown type).
    """
    if path.name == "MEMORY.md":
        return None
    text = path.read_text()
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return None
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict):
        return None
    type_ = meta.get("type") or (meta.get("metadata") or {}).get("type")
    description = str(meta.get("description") or "").strip()
    if type_ not in TYPES or not description:
        return None
    return ImportedMemory(
        name=slugify(str(meta.get("name") or path.stem)),
        type=type_,
        description=description,
        body=m.group(2).strip(),
        source=path,
    )


def claude_memory_files(paths: list[Path]) -> list[Path]:
    """Expand files and memory directories into individual memory files."""
    out: list[Path] = []
    for p in paths:
        p = p.expanduser()
        out += sorted(p.glob("*.md")) if p.is_dir() else [p]
    return out
