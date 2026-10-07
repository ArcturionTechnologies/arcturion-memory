"""health: score each agent's MEMORY.md index on six criteria. Read-only.

Adapted from a CLAUDE.md-improver style rubric. Max 100 points:
  1. Completeness          index covers every sibling .md file        (20)
  2. Currency              index is not older than its newest sibling (20)
  3. Sortedness            pinned / load-bearing entries come first   (15)
  4. Hook quality          every entry has a short description        (15)
  5. Frontmatter alignment entry names match each file's `name`       (15)
  6. Conciseness           under ~200 lines and 25 KB                  (15)

Grades: A 90-100, B 70-89, C 50-69, D 30-49, F 0-29.

  python3 -m arcturion_memory health [--config FILE]            # every configured agent
  python3 -m arcturion_memory health --agent builder --verbose
  python3 -m arcturion_memory health --include-harness           # also harness_globs folders
  python3 -m arcturion_memory health --json
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .common import file_mtime, parse_frontmatter, short_path
from .config import Config, load_config

INDEX_FILE = "MEMORY.md"
# Match standard markdown `[name](href)` AND Obsidian `[[stem]]` wikilinks
ENTRY_MD_RE = re.compile(r"^- (?:📌\s+)?(?:🛡\s+)?(?:🍂\s+)?\[(?P<name>[^\]]+)\]\((?P<href>[^\)]+)\)(?:\s*[—-]\s*(?P<hook>.+))?$")
ENTRY_WIKI_RE = re.compile(r"^- (?:📌\s+)?(?:🛡\s+)?(?:🍂\s+)?\[\[(?P<href>[^\]]+)\]\](?:\s*[—-]\s*(?P<hook>.+))?$")


def parse_entry(line: str) -> dict | None:
    s = line.strip()
    m = ENTRY_MD_RE.match(s)
    if m:
        href = m.group("href").strip()
        return {"name": m.group("name").strip(), "href": href,
                "hook": (m.group("hook") or "").strip(), "raw": s}
    m = ENTRY_WIKI_RE.match(s)
    if m:
        stem = m.group("href").strip()
        # Obsidian wikilink — file is `<stem>.md`; name = stem (humanized later)
        href = stem if stem.endswith(".md") else f"{stem}.md"
        return {"name": stem.replace("_", " ").replace("-", " "),
                "href": href, "hook": (m.group("hook") or "").strip(), "raw": s,
                "wiki": True}
    return None
MAX_HOOK = 150
MAX_LINES = 200
MAX_BYTES = 25 * 1024


def score_one(cfg: Config, memory_dir: Path) -> dict:
    target = memory_dir / INDEX_FILE
    if not target.exists():
        return {"path": str(target), "exists": False, "score": 0, "grade": "F",
                "issues": ["MEMORY.md missing"], "suggestions": ["create it: python3 -m arcturion_memory index"]}

    text = target.read_text(errors="replace")
    fm, body = parse_frontmatter(text)

    # Sibling files (excluding the index itself + non-stamp files)
    siblings = [p for p in memory_dir.glob("*.md")
                if p.name not in cfg.no_stamp_filenames and not cfg.is_skipped(p)]
    sibling_names = {p.name for p in siblings}

    # Parse entries (supports both markdown links + Obsidian wikilinks)
    entries: list[dict] = []
    for ln in body.splitlines():
        e = parse_entry(ln)
        if e:
            entries.append(e)

    issues: list[str] = []
    suggestions: list[str] = []
    scores: dict = {}

    # 1. Completeness — entries point to existing siblings, all siblings indexed
    indexed_hrefs = {e["href"] for e in entries}
    missing_from_index = sibling_names - indexed_hrefs
    # An entry is broken only if its file genuinely does not exist. Curated hubs
    # link into category subdirs (e.g. `People/x.md`); those are valid even
    # though they are not top-level siblings, so resolve the href on disk rather
    # than requiring membership in the top-level sibling set.
    broken_entries = [e for e in entries if not (memory_dir / e["href"]).exists()]
    completeness = 20
    if siblings:
        coverage = len(indexed_hrefs & sibling_names) / max(len(sibling_names), 1)
        completeness = round(20 * coverage)
        if missing_from_index:
            issues.append(f"{len(missing_from_index)} sibling(s) not in index: {sorted(missing_from_index)[:3]}")
            suggestions.append("regenerate: python3 -m arcturion_memory index")
        if broken_entries:
            issues.append(f"{len(broken_entries)} entry(ies) point to missing files")
    scores["completeness"] = completeness

    # 2. Currency — files referenced by index were modified recently relative to index mtime
    currency = 20
    if entries:
        index_mtime = file_mtime(target)
        sibling_max_mtime = max((file_mtime(p) for p in siblings), default=index_mtime)
        if sibling_max_mtime > index_mtime:
            stale_days = (sibling_max_mtime - index_mtime).days
            if stale_days >= 14:
                currency = 5
                issues.append(f"index is {stale_days}d behind newest sibling — definitely stale")
                suggestions.append("regenerate: python3 -m arcturion_memory index")
            elif stale_days >= 3:
                currency = 12
                issues.append(f"index is {stale_days}d behind newest sibling")
            else:
                currency = 18
    scores["currency"] = currency

    # 3. Sortedness — verify pinned/load-bearing markers come first
    sortedness = 15
    saw_pinned = False
    saw_load_bearing = False
    saw_lower_priority = False
    for e in entries:
        raw = e["raw"]
        if "📌" in raw:
            if saw_lower_priority:
                sortedness = max(sortedness - 3, 0)
                issues.append(f"📌 entry after lower-priority entry: {e['name'][:40]}")
            saw_pinned = True
        elif "🛡" in raw:
            if saw_lower_priority and not saw_pinned:
                sortedness = max(sortedness - 2, 0)
            saw_load_bearing = True
        elif "🍂" in raw:
            saw_lower_priority = True
        else:
            saw_lower_priority = True
    scores["sortedness"] = sortedness

    # 4. Hook quality — descriptions present + length
    hook_quality = 15
    no_hook = sum(1 for e in entries if not e["hook"])
    long_hooks = sum(1 for e in entries if len(e["hook"]) > MAX_HOOK)
    if entries:
        hook_quality = round(15 * (1 - (no_hook + long_hooks * 0.5) / max(len(entries), 1)))
        if no_hook:
            issues.append(f"{no_hook} entries missing hooks")
        if long_hooks:
            issues.append(f"{long_hooks} hooks > {MAX_HOOK} chars")
    scores["hook_quality"] = max(hook_quality, 0)

    # 5. Frontmatter alignment — entry name == file fm.name (when file has fm)
    alignment = 15
    if entries:
        mismatches = 0
        for e in entries:
            target_path = memory_dir / e["href"]
            if not target_path.exists():
                continue
            try:
                t_fm, _ = parse_frontmatter(target_path.read_text(errors="replace"))
                fm_name = (t_fm.get("name") or "").strip()
                if fm_name and fm_name.lower() != e["name"].lower() and e["name"].lower() not in fm_name.lower():
                    mismatches += 1
            except Exception:
                continue
        if entries:
            alignment = round(15 * (1 - mismatches / max(len(entries), 1)))
            if mismatches:
                issues.append(f"{mismatches} entries' display name diverges from file frontmatter `name`")
                suggestions.append("regenerate: python3 -m arcturion_memory index (pulls fresh `name` from frontmatter)")
    scores["alignment"] = max(alignment, 0)

    # 6. Conciseness — line count + byte size
    line_count = len(body.splitlines())
    byte_size = len(text.encode("utf-8"))
    conciseness = 15
    if line_count > MAX_LINES:
        conciseness -= 5
        issues.append(f"{line_count} lines (target ≤{MAX_LINES})")
    if byte_size > MAX_BYTES:
        conciseness -= 5
        issues.append(f"{byte_size // 1024}KB (target ≤{MAX_BYTES // 1024}KB)")
    scores["conciseness"] = max(conciseness, 0)

    total = sum(scores.values())
    grade = "F"
    for thresh, g in [(90, "A"), (70, "B"), (50, "C"), (30, "D")]:
        if total >= thresh:
            grade = g
            break

    return {
        "path": str(target), "exists": True, "score": total, "grade": grade,
        "scores": scores, "entry_count": len(entries), "sibling_count": len(siblings),
        "issues": issues, "suggestions": suggestions,
    }


def all_targets(cfg: Config, vault_only=False, harness_only=False, include_harness=False) -> list[tuple[str, Path]]:
    """(label, memory dir) pairs to score.

    Default scope is each configured agent's Memory folder. Folders matched by
    harness_globs (for example per-project harness memory) are scanned only when
    asked for with --include-harness or --harness-only.
    """
    targets: list[tuple[str, Path]] = []
    if not harness_only:
        targets += cfg.memory_dirs()
    if harness_only or (include_harness and not vault_only):
        targets += [("harness", d) for d in cfg.harness_dirs()]
    return targets


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="arcturion_memory health", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="config JSON (else $ARC_MEMORY_CONFIG)")
    ap.add_argument("--agent", help="restrict to one agent")
    ap.add_argument("--vault-only", action="store_true")
    ap.add_argument("--harness-only", action="store_true")
    ap.add_argument("--include-harness", action="store_true",
                    help="also score folders matched by harness_globs")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    targets = all_targets(cfg, args.vault_only, args.harness_only, args.include_harness)
    if args.agent:
        targets = [t for t in targets if t[0].lower() == args.agent.lower()]

    reports = []
    for label, d in targets:
        r = score_one(cfg, d)
        r["agent"] = label
        reports.append(r)

    if args.json:
        print(json.dumps(reports, indent=2, default=str))
        return 0

    avg = sum(r["score"] for r in reports) / max(len(reports), 1)
    print(f"MEMORY.md health: {len(reports)} indexes scored, avg {avg:.0f}/100\n")
    print(f"{'Grade':<6}{'Score':<8}{'Entries':<10}{'Siblings':<10}{'Agent':<14}Path")
    print("-" * 100)
    for r in sorted(reports, key=lambda x: -x["score"]):
        path_short = short_path(r["path"])[-70:]
        if not r["exists"]:
            print(f"{'F':<6}{0:<8}{'-':<10}{'-':<10}{r['agent']:<14}{path_short}  MISSING")
            continue
        print(f"{r['grade']:<6}{r['score']:<8}{r['entry_count']:<10}{r['sibling_count']:<10}{r['agent']:<14}{path_short}")
        if args.verbose or r["score"] < 70:
            for iss in r["issues"][:5]:
                print(f"      !  {iss}")
            for sug in r["suggestions"][:3]:
                print(f"      >  {sug}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
