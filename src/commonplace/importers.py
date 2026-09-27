"""Import memories written by other systems (currently Claude Code auto-memory)."""

from __future__ import annotations

import json
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


def _encode(path: str) -> str:
    """Claude Code's project-directory encoding: every non-alphanumeric becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def _cwd_from_transcripts(project_dir: Path) -> Path | None:
    for transcript in sorted(project_dir.glob("*.jsonl")):
        with transcript.open() as f:
            for line in f:
                if '"cwd"' not in line:
                    continue
                try:
                    cwd = json.loads(line).get("cwd")
                except json.JSONDecodeError:
                    continue
                if cwd:
                    return Path(cwd)
    return None


def _decode_by_walking(encoded: str, root: Path = Path("/")) -> Path | None:
    """Rebuild the path an encoded project-dir name stands for by walking the filesystem.

    The encoding is lossy ('/', '.', '-' all become '-'), so match directory
    entries against the encoded remainder, preferring the longest match.
    """
    if encoded in ("", "-"):
        return root
    try:
        children = sorted((c for c in root.iterdir() if c.is_dir()), key=lambda c: -len(c.name))
    except OSError:
        return None
    for child in children:
        token = "-" + _encode(child.name)
        if encoded == token:
            return child
        if encoded.startswith(token + "-"):
            if found := _decode_by_walking(encoded[len(token):], child):
                return found
    return None


def claude_project_path(project_dir: Path) -> Path | None:
    """The working directory a Claude Code project dir (~/.claude/projects/<encoded>) belongs to.

    Transcripts record the cwd exactly; without them, decode the directory
    name against the filesystem. None if the directory no longer exists.
    """
    cwd = _cwd_from_transcripts(project_dir)
    if cwd is not None and cwd.is_dir():
        return cwd
    return _decode_by_walking(project_dir.name)
