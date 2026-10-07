"""Configuration for ArcturionMemory.

Every tool reads one JSON config file. Where it comes from, in order:
  1. the --config flag
  2. the ARC_MEMORY_CONFIG environment variable
  3. built-in defaults (agents discovered under ARC_ROOT, or the current folder)

Paths may use ~ and $VARS. Relative paths resolve against the config file's
folder. See examples/config.example.json for every key.
"""
from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_TTL_BY_TYPE = {
    "user": 365, "feedback": 365, "project": 90, "reference": 180,
    "state": 30, "policy": -1, "adr": -1, "log": -1,
    "briefing": 7, "digest": 30, "scan": 14, "research": 30,
}
DEFAULT_SKIP_PARTS = ["/.git/", "/__pycache__/", "/node_modules/", "/.venv/", "/venv/"]
DEFAULT_NO_STAMP = ["MEMORY.md", "INDEX.md", "README.md", "AGENTS.md"]
DEFAULT_LOAD_BEARING_PATH_REGEX = r"(legal|contract|credential|adr|north-star|mandate|runbook|policy)"
DEFAULT_LOAD_BEARING_FILENAMES = [
    r"^north-star-.*\.md$", r"^feedback_.*\.md$", r"^user_.*\.md$",
    r"^(decision|adr)-.*\.md$", r"^runbook-.*\.md$", r"^policy-.*\.md$",
]
DEFAULT_EPHEMERAL_FILENAMES = [
    r"^briefing-.*\.md$", r"^morning-.*\.md$", r"^digest-.*\.md$",
    r".*-scan\.md$", r"^\d{4}-\d{2}-\d{2}-.*-session\.md$",
]
# Markers of a populated agent home. When several folders match inner_glob,
# one carrying either marker wins over an empty look-alike.
INNER_HOME_MARKERS = ("Boot", "Memory")


def _expand(value: str, base: Path) -> Path:
    p = Path(os.path.expandvars(os.path.expanduser(str(value))))
    return p if p.is_absolute() else (base / p)


@dataclass
class Config:
    agents_root: Path
    agents: list[str] | None = None          # None = discover sub-folders of agents_root
    inner_glob: str | None = None            # e.g. "000 *" when memory sits one level deeper
    memory_dirname: str = "Memory"
    extra_crawl_roots: list[Path] = field(default_factory=list)
    harness_globs: list[str] = field(default_factory=list)
    skip_parts: list[str] = field(default_factory=lambda: list(DEFAULT_SKIP_PARTS))
    read_only_prefixes: list[str] = field(default_factory=list)
    dedup_skip_prefixes: list[str] = field(default_factory=list)
    no_stamp_filenames: set[str] = field(default_factory=lambda: set(DEFAULT_NO_STAMP))
    load_bearing_path_regex: str = DEFAULT_LOAD_BEARING_PATH_REGEX
    load_bearing_filenames: list[str] = field(default_factory=lambda: list(DEFAULT_LOAD_BEARING_FILENAMES))
    load_bearing_prefixes: list[str] = field(default_factory=list)
    ephemeral_filenames: list[str] = field(default_factory=lambda: list(DEFAULT_EPHEMERAL_FILENAMES))
    ttl_by_type: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_TTL_BY_TYPE))
    grace_days: int = 30
    extra_owner_agents: list[str] = field(default_factory=lambda: ["shared", "unknown"])
    domain_owners: list[dict[str, Any]] = field(default_factory=list)
    recall_index_files: list[Path] = field(default_factory=list)
    recall_roots: list[Path] = field(default_factory=list)

    # -- compiled helpers ----------------------------------------------------
    def __post_init__(self) -> None:
        self._lb_path = re.compile(self.load_bearing_path_regex, re.I) if self.load_bearing_path_regex else None
        self._lb_names = [re.compile(p, re.I) for p in self.load_bearing_filenames]
        self._eph_names = [re.compile(p, re.I) for p in self.ephemeral_filenames]

    # -- agent resolution ----------------------------------------------------
    def agent_names(self) -> list[str]:
        if self.agents is not None:
            return list(self.agents)
        if not self.agents_root.is_dir():
            return []
        return sorted(p.name for p in self.agents_root.iterdir()
                      if p.is_dir() and not p.name.startswith((".", "_")))

    def agent_home(self, name: str) -> Path | None:
        home = self.agents_root / name
        return home if home.is_dir() else None

    def agent_inner_dir(self, name: str) -> Path | None:
        """The folder that holds Memory/ for an agent.

        Without inner_glob that is the agent home itself. With inner_glob, an
        agent can carry several matching folders, and plain-ASCII names sort
        before emoji ones, so "first match" can pick an empty look-alike. Prefer
        a candidate that is actually populated (has Boot/ or Memory/), and fall
        back to the first match only when none qualifies.
        """
        home = self.agent_home(name)
        if home is None:
            return None
        if not self.inner_glob:
            return home
        candidates = sorted(p for p in home.glob(self.inner_glob) if p.is_dir())
        for c in candidates:
            if any((c / m).is_dir() for m in INNER_HOME_MARKERS):
                return c
        return candidates[0] if candidates else None

    def agent_memory_dir(self, name: str) -> Path | None:
        inner = self.agent_inner_dir(name)
        mem = inner / self.memory_dirname if inner else None
        return mem if mem and mem.is_dir() else None

    def memory_dirs(self) -> list[tuple[str, Path]]:
        out = []
        for name in self.agent_names():
            d = self.agent_memory_dir(name)
            if d is not None:
                out.append((name, d))
        return out

    def harness_dirs(self) -> list[Path]:
        out: list[Path] = []
        for pattern in self.harness_globs:
            expanded = os.path.expandvars(os.path.expanduser(pattern))
            out.extend(sorted(Path(p) for p in glob.glob(expanded) if Path(p).is_dir()))
        return out

    def crawl_roots(self) -> list[Path]:
        roots = [d for _, d in self.memory_dirs()]
        roots += list(self.extra_crawl_roots)
        roots += self.harness_dirs()
        return roots

    def owner_agents(self) -> set[str]:
        return {a.lower() for a in self.agent_names()} | {a.lower() for a in self.extra_owner_agents}

    # -- path policy ---------------------------------------------------------
    def is_skipped(self, path: Path) -> bool:
        p = str(path)
        return any(part in p for part in self.skip_parts)

    def is_read_only(self, path: Path) -> bool:
        p = str(path)
        return any(p.startswith(pre) for pre in self.read_only_prefixes)

    def is_dedup_skipped(self, path: Path) -> bool:
        p = str(path)
        return any(p.startswith(pre) for pre in self.dedup_skip_prefixes)

    def is_load_bearing(self, path: Path) -> tuple[bool, str]:
        p = str(path)
        for prefix in self.load_bearing_prefixes:
            if p.startswith(prefix):
                return True, f"path prefix: {prefix}"
        if self._lb_path and self._lb_path.search(path.name):
            return True, "filename keyword match"
        for pat in self._lb_names:
            if pat.match(path.name):
                return True, f"filename: {pat.pattern}"
        return False, ""

    def is_ephemeral_by_name(self, path: Path) -> bool:
        return any(p.match(path.name) for p in self._eph_names)


def _paths(values, base: Path) -> list[Path]:
    return [_expand(v, base) for v in (values or [])]


def _prefixes(values, base: Path) -> list[str]:
    return [str(_expand(v, base)) for v in (values or [])]


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Load a config file, or build defaults when none is given."""
    path = path or os.environ.get("ARC_MEMORY_CONFIG")
    if not path:
        root = Path(os.environ.get("ARC_ROOT") or ".").expanduser()
        return Config(agents_root=root.resolve())
    cfg_path = Path(path).expanduser().resolve()
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    base = cfg_path.parent
    kw: dict[str, Any] = {
        "agents_root": _expand(raw.get("agents_root") or os.environ.get("ARC_ROOT") or ".", base).resolve(),
        "agents": raw.get("agents"),
        "inner_glob": raw.get("inner_glob"),
        "memory_dirname": raw.get("memory_dirname", "Memory"),
        "extra_crawl_roots": _paths(raw.get("extra_crawl_roots"), base),
        "harness_globs": list(raw.get("harness_globs") or []),
        "read_only_prefixes": _prefixes(raw.get("read_only_prefixes"), base),
        "dedup_skip_prefixes": _prefixes(raw.get("dedup_skip_prefixes"), base),
        "load_bearing_prefixes": _prefixes(raw.get("load_bearing_prefixes"), base),
        "domain_owners": list(raw.get("domain_owners") or []),
        "recall_index_files": _paths(raw.get("recall_index_files"), base),
        "recall_roots": _paths(raw.get("recall_roots"), base),
    }
    for key in ("skip_parts", "load_bearing_filenames", "ephemeral_filenames", "extra_owner_agents"):
        if key in raw:
            kw[key] = list(raw[key])
    if "no_stamp_filenames" in raw:
        kw["no_stamp_filenames"] = set(raw["no_stamp_filenames"])
    if "load_bearing_path_regex" in raw:
        kw["load_bearing_path_regex"] = raw["load_bearing_path_regex"]
    if "ttl_by_type" in raw:
        kw["ttl_by_type"] = {**DEFAULT_TTL_BY_TYPE, **raw["ttl_by_type"]}
    if "grace_days" in raw:
        kw["grace_days"] = int(raw["grace_days"])
    return Config(**kw)
