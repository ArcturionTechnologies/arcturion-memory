"""index: curation-aware regeneration of each agent's MEMORY.md hub.

Agents often organize memory into category sub-folders (People, Preferences,
Decisions, Pointers, Sessions, ...) with a MEMORY.md hub that points into them.
A naive indexer flattens that. This one regenerates the hub without losing it:

  - Targets the same Memory folders that `health` scores.
  - Keeps the hub's raw frontmatter, H1 title and intro blurb verbatim (no
    serializer round-trip, which would mangle nested `metadata:` blocks).
  - Emits a `## 📂 Root` section for top-level files, then one section per
    non-empty category sub-folder.
  - Hoists pinned / load-bearing entries into one `## 📌 Pinned & Load-bearing`
    section first, so "load-bearing first" holds across the whole document.
  - Large sub-folders (> INLINE_MAX files), or any that already have an
    INDEX.md, are linked by pointer. An existing INDEX.md is NEVER overwritten
    (it may be hand-ordered); a missing one for a large folder is generated.
  - Backs up the previous hub to MEMORY.md.bak-curation-<date> before writing.

Only real files on disk are indexed. Nothing is invented.

  python3 -m arcturion_memory index [--config FILE]       # every configured agent
  python3 -m arcturion_memory index --agent builder
  python3 -m arcturion_memory index --dry-run             # preview, write nothing
  python3 -m arcturion_memory index --include-harness     # also harness_globs folders
"""
from __future__ import annotations

import argparse
import datetime
from pathlib import Path

from .common import FM_FENCE, parse_frontmatter, short_path
from .config import Config, load_config

INDEX_FILE = "MEMORY.md"      # the hub, at the top of a Memory dir
SUBINDEX_FILE = "INDEX.md"    # per-category index inside a subdir
MAX_HOOK_CHARS = 130          # keep under the health check's 150-char ceiling
INLINE_MAX = 40               # subdirs larger than this use an INDEX.md pointer
BACKUP_SUFFIX = f".bak-curation-{datetime.date.today():%Y%m%d}"

# Category subdir → section header. Unlisted subdirs get a generic 📦 header.
CATEGORY_HEADERS = {
    "people": "## 🧑 People",
    "preferences": "## 🎯 Preferences",
    "decisions": "## ⚖️ Decisions",
    "pointers": "## 📍 Pointers",
    "sessions": "## 📓 Sessions",
    "domains": "## 🌐 Domains",
    "telegram": "## ✈️ Telegram",
    "chats": "## 💬 Chats",
    "approvals": "## ✅ Approvals",
    "research": "## 🔬 Research",
    "projects": "## 🚀 Projects",
    "brand": "## ✨ Brand",
    "feedback": "## 🗣 Feedback",
}
# Fixed display order for the well-known categories; extras follow alphabetically.
CATEGORY_ORDER = ["people", "preferences", "decisions", "pointers", "sessions"]


def first_heading(body: str) -> str:
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
        if s.startswith("## "):
            return s[3:].strip()
    return ""


def short_hook(fm: dict, body: str) -> str:
    desc = (fm.get("description") or "").strip().strip('"')
    if desc:
        return desc[:MAX_HOOK_CHARS].rstrip(".")
    h = first_heading(body)
    if h:
        return h[:MAX_HOOK_CHARS]
    for ln in body.splitlines():
        s = ln.strip()
        if s and not s.startswith("#") and not s.startswith("---") and not s.startswith(">"):
            return s[:MAX_HOOK_CHARS].rstrip(".")
    return ""


def extract_entry(cfg: Config, path: Path) -> dict | None:
    """Parse one memory file into an index entry, or None to skip."""
    if path.name in cfg.no_stamp_filenames or path.name == SUBINDEX_FILE:
        return None
    try:
        text = path.read_text(errors="replace")
    except Exception:
        return None
    fm, body = parse_frontmatter(text)
    name = (fm.get("name") or "").strip().strip('"') or first_heading(body) or path.stem
    crit = str(fm.get("criticality", "")).strip().lower()
    pinned = str(fm.get("pinned", "")).strip().lower() in ("true", "yes", "1")
    load_bearing = crit == "load-bearing" or cfg.is_load_bearing(path)[0]
    return {
        "path": path,
        "name": name[:80],
        "pinned": pinned,
        "load_bearing": load_bearing,
        "hook": short_hook(fm, body),
    }


def render_entry(e: dict, rel_href: str) -> str:
    if e["pinned"]:
        marker = "📌 "
    elif e["load_bearing"]:
        marker = "🛡 "
    else:
        marker = ""
    line = f"- {marker}[{e['name']}]({rel_href})"
    if e["hook"]:
        line += f" — {e['hook']}"
    return line


def sort_entries(entries: list[dict]) -> list[dict]:
    """pinned → load-bearing → alpha(name)."""
    return sorted(entries, key=lambda e: (
        0 if e["pinned"] else (1 if e["load_bearing"] else 2),
        e["name"].lower(),
    ))


def collect(cfg: Config, dir_: Path) -> list[dict]:
    """Index entries for the *.md files directly inside `dir_` (non-recursive)."""
    out = []
    for p in sorted(dir_.glob("*.md")):
        if cfg.is_skipped(p):
            continue
        e = extract_entry(cfg, p)
        if e:
            out.append(e)
    return out


def category_header(subname: str) -> str:
    key = subname.lower()
    if key in CATEGORY_HEADERS:
        return CATEGORY_HEADERS[key]
    return f"## 📦 {subname}"


def split_header(text: str, agent_disp: str, agent_emoji: str) -> str:
    """Return the preserved hub header: raw frontmatter + H1 title + intro blurb.

    Everything from the first `## ` section (or first `- ` list item) onward is
    dropped — that's the regenerated body.
    """
    m = FM_FENCE.match(text)
    fm_raw = m.group(0) if m else ""
    rest = text[m.end():] if m else text

    kept: list[str] = []
    for line in rest.splitlines():
        s = line.strip()
        if s.startswith("## ") or s.startswith("- "):
            break
        kept.append(line)
    # Trim trailing blank lines from kept block.
    while kept and not kept[-1].strip():
        kept.pop()

    has_h1 = any(ln.strip().startswith("# ") for ln in kept)
    blocks = []
    if fm_raw:
        blocks.append(fm_raw.rstrip())
    if not has_h1:
        blocks.append(f"# {agent_emoji} {agent_disp} — Memory Index".rstrip())
    if kept:
        blocks.append("\n".join(kept))
    return "\n\n".join(b for b in blocks if b).rstrip()


def regen_index(cfg: Config, memory_dir: Path, dry: bool = False) -> dict:
    """Regenerate `memory_dir/MEMORY.md`. Returns a small report dict."""
    target = memory_dir / INDEX_FILE
    inner = memory_dir.parent.name  # e.g. "000 🤖 Builder", or just the agent name
    parts = inner.split(" ", 2)
    if len(parts) == 3:          # "<prefix> <emoji> <Name>" inner-home layout
        agent_emoji, agent_disp = parts[1], parts[2]
    else:                        # flat layout: the folder is the agent name
        agent_emoji, agent_disp = "🧠", inner

    # --- gather ---------------------------------------------------------------
    root_entries = collect(cfg, memory_dir)

    subdirs: dict[str, dict] = {}  # name -> {"entries":[...], "total":int, "use_pointer":bool, "gen":bool}
    for sub in sorted(memory_dir.iterdir()):
        if not sub.is_dir() or sub.name.startswith("_") or sub.name.startswith("."):
            continue
        ents = collect(cfg, sub)
        total = len(ents)
        if total == 0:
            continue
        idx = sub / SUBINDEX_FILE
        use_pointer = total > INLINE_MAX or idx.exists()
        subdirs[sub.name] = {
            "entries": ents, "total": total,
            "use_pointer": use_pointer,
            "gen_index": use_pointer and not idx.exists(),
            "path": sub,
        }

    # --- hoist pinned / load-bearing into a single top section ----------------
    def relhref(e: dict) -> str:
        return str(e["path"].relative_to(memory_dir))

    hoisted: list[tuple[dict, str]] = []
    for e in root_entries:
        if e["pinned"] or e["load_bearing"]:
            hoisted.append((e, relhref(e)))
    # Subdir pinned entries are hoisted only from INLINE subdirs (pointer subdirs
    # keep their own ordering inside the INDEX.md they reference).
    for name, info in subdirs.items():
        if info["use_pointer"]:
            continue
        for e in info["entries"]:
            if e["pinned"] or e["load_bearing"]:
                hoisted.append((e, relhref(e)))
    hoisted_paths = {id(e) for e, _ in hoisted}

    # --- build sections -------------------------------------------------------
    sections: list[str] = []

    if hoisted:
        ordered = sorted(hoisted, key=lambda t: (
            0 if t[0]["pinned"] else 1, t[0]["name"].lower()))
        lines = ["## 📌 Pinned & Load-bearing"]
        lines += [render_entry(e, href) for e, href in ordered]
        sections.append("\n".join(lines))

    root_remaining = [e for e in root_entries if id(e) not in hoisted_paths]
    if root_remaining:
        lines = ["## 📂 Root"]
        lines += [render_entry(e, e["path"].name) for e in sort_entries(root_remaining)]
        sections.append("\n".join(lines))

    # Fixed order for known categories; extras follow alphabetically.
    lower_to_actual = {k.lower(): k for k in subdirs}
    seen = set()
    final_order = []
    for ln in CATEGORY_ORDER:
        if ln in lower_to_actual:
            final_order.append(lower_to_actual[ln]); seen.add(lower_to_actual[ln])
    for k in sorted(subdirs):
        if k not in seen:
            final_order.append(k)

    for name in final_order:
        info = subdirs[name]
        header = category_header(name)
        if info["use_pointer"]:
            if info["gen_index"] and not dry:
                _write_subindex(info["path"], info["entries"])
            sections.append(
                f"{header}\n- **{info['total']} entries** → "
                f"[{name}/INDEX.md]({name}/INDEX.md) — read on demand")
        else:
            remaining = [e for e in info["entries"] if id(e) not in hoisted_paths]
            if not remaining and not info["entries"]:
                continue
            lines = [header]
            for e in sort_entries(remaining):
                lines.append(render_entry(e, str(e["path"].relative_to(memory_dir))))
            if len(lines) == 1:
                # all entries were hoisted; note the pointer to keep section meaningful
                lines.append(f"- _(all {info['total']} pinned above)_")
            sections.append("\n".join(lines))

    header_block = split_header(target.read_text(errors="replace") if target.exists() else "",
                                agent_disp, agent_emoji)
    content = header_block.rstrip() + "\n\n" + "\n\n".join(sections) + "\n"

    n_entries = len(root_entries) + sum(i["total"] for i in subdirs.values())
    report = {"path": str(target), "root": len(root_entries),
              "subdirs": {k: v["total"] for k, v in subdirs.items()},
              "hoisted": len(hoisted), "entries": n_entries}

    if dry:
        report["dry"] = True
        return report

    if target.exists():
        backup = target.with_name(target.name + BACKUP_SUFFIX)
        if not backup.exists():
            backup.write_text(target.read_text(errors="replace"))
    target.write_text(content)
    return report


def _write_subindex(subdir: Path, entries: list[dict]) -> None:
    """Generate a missing INDEX.md for a large subdir (flat, pinned-first→alpha)."""
    title = subdir.name
    lines = [f"# {title} — Index", ""]
    for e in sort_entries(entries):
        lines.append(render_entry(e, e["path"].name))
    (subdir / SUBINDEX_FILE).write_text("\n".join(lines) + "\n")


def all_targets(cfg: Config, include_harness=False, harness_only=False) -> list[tuple[str, Path]]:
    targets: list[tuple[str, Path]] = []
    if not harness_only:
        targets += cfg.memory_dirs()
    if harness_only or include_harness:
        targets += [("harness", d) for d in cfg.harness_dirs()]
    return targets


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="arcturion_memory index", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="config JSON (else $ARC_MEMORY_CONFIG)")
    ap.add_argument("--agent", help="restrict to one agent")
    ap.add_argument("--include-harness", action="store_true")
    ap.add_argument("--harness-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    targets = all_targets(cfg, args.include_harness, args.harness_only)
    if args.agent:
        targets = [t for t in targets if t[0].lower() == args.agent.lower()]
    if not targets:
        print(f"no memory folders matched (--agent={args.agent})")
        return 1

    total = 0
    for label, t in targets:
        r = regen_index(cfg, t, dry=args.dry_run)
        subs = ", ".join(f"{k}:{v}" for k, v in r["subdirs"].items()) or "-"
        print(f"  {'[dry] ' if args.dry_run else ''}{r['entries']:4d} entries "
              f"(root {r['root']}, pinned {r['hoisted']}) -> {short_path(r['path'])}")
        print(f"        subdirs: {subs}")
        total += r["entries"]
    print(f"\n{len(targets)} indexes processed, {total} total entries indexed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
