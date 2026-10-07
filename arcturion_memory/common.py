"""Shared helpers: frontmatter parsing, timestamps, file iteration."""
from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any, Iterator

from .config import Config

FM_FENCE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Flat `key: value` frontmatter. Nested blocks are kept as raw strings."""
    m = FM_FENCE.match(text)
    if not m:
        return {}, text
    fm: dict[str, Any] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        fm[k.strip()] = v.strip()
    return fm, text[m.end():]


KEYS_ORDER = [
    "name", "description", "type", "created", "last_verified", "verified_by",
    "ttl_days", "criticality", "owner_agent", "pinned", "tags", "inbound_refs",
]


def serialize_frontmatter(fm: dict[str, Any], body: str) -> str:
    seen = set()
    lines = ["---"]
    for k in KEYS_ORDER:
        if k in fm:
            lines.append(f"{k}: {fm[k]}")
            seen.add(k)
    for k, v in fm.items():
        if k not in seen:
            lines.append(f"{k}: {v}")
    lines.append("---\n")
    return "\n".join(lines) + body


def parse_iso(s: str) -> datetime.datetime | None:
    if not s:
        return None
    s = s.strip().strip("'\"")
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except Exception:
        try:
            return datetime.datetime.fromisoformat(s.split("T")[0]).replace(tzinfo=datetime.timezone.utc)
        except Exception:
            return None


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def file_mtime(path: Path) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(path.stat().st_mtime, tz=datetime.timezone.utc)


def freshness_signal(path: Path, fm: dict | None = None) -> datetime.datetime:
    """max(mtime, last_verified): a file with an old mtime but a recent
    last_verified stamp was confirmed still accurate, so it is not stale."""
    mt = file_mtime(path)
    if fm is None:
        try:
            fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        except Exception:
            return mt
    lv = parse_iso(str(fm.get("last_verified", ""))) if fm else None
    return max(mt, lv) if lv else mt


def iter_md_files(cfg: Config, root: Path) -> Iterator[Path]:
    """Every *.md under root, minus skipped paths and index files."""
    if not root.exists():
        return
    for p in sorted(root.rglob("*.md")):
        if cfg.is_skipped(p) or p.name in cfg.no_stamp_filenames:
            continue
        yield p


def short_path(path: str | Path) -> str:
    """Path relative to the working folder when inside it, else with ~ for home."""
    s = str(path)
    try:
        return str(Path(s).resolve().relative_to(Path.cwd().resolve()))
    except (ValueError, OSError):
        pass
    home = str(Path.home())
    return "~" + s[len(home):] if s.startswith(home) else s
