"""audit: frontmatter linter across every crawl root.

Reports:
  - missing required keys (name, type, created)
  - malformed YAML lines inside the frontmatter fence
  - expired TTLs (now - last_verified > ttl_days + grace; ephemeral files only)
  - owner_agent values that are not a configured agent
  - unknown type / criticality values
  - pinned files whose criticality isn't load-bearing
  - created dates in the future

Read-only unless you pass --patch --apply, which stamps a missing `created`
from the file's mtime (never inside read_only_prefixes).

  python3 -m arcturion_memory audit [--config FILE] [--root DIR] [--json]
  python3 -m arcturion_memory audit --patch            # preview stamps
  python3 -m arcturion_memory audit --patch --apply    # write stamps
"""
from __future__ import annotations

import argparse
import datetime
import json
from collections import Counter, defaultdict
from pathlib import Path

from .common import (file_mtime, iter_md_files, now, parse_frontmatter, parse_iso,
                     serialize_frontmatter)
from .config import Config, load_config

REQUIRED_KEYS = ["name", "type", "created"]
EXTRA_TYPES = {"skill", "agent", "session", "system"}
VALID_CRIT = {"load-bearing", "reference", "ephemeral"}


def lint_one(cfg: Config, path: Path) -> list[dict]:
    """Findings for a single file. Files without frontmatter are not findings."""
    findings: list[dict] = []
    try:
        text = path.read_text(errors="replace")
    except Exception as e:
        return [{"path": str(path), "issue": "unreadable", "detail": str(e)}]

    fm, _body = parse_frontmatter(text)
    if not fm:
        return []

    fm_text = text.split("---", 2)[1] if text.startswith("---") else ""
    for ln in fm_text.splitlines():
        s = ln.strip()
        if s and ":" not in s and not s.startswith("-") and not s.startswith("#"):
            findings.append({"path": str(path), "issue": "malformed_yaml_line", "detail": s[:80]})

    for k in REQUIRED_KEYS:
        if not str(fm.get(k, "")).strip():
            findings.append({"path": str(path), "issue": "missing_required", "detail": k})

    t = (fm.get("type") or "").strip().lower()
    if t and t not in set(cfg.ttl_by_type) | EXTRA_TYPES:
        findings.append({"path": str(path), "issue": "invalid_type", "detail": t})

    oa = (fm.get("owner_agent") or "").strip().lower()
    if oa and oa not in cfg.owner_agents():
        findings.append({"path": str(path), "issue": "invalid_owner_agent", "detail": oa})

    crit = (fm.get("criticality") or "").strip().lower()
    if crit and crit not in VALID_CRIT:
        findings.append({"path": str(path), "issue": "invalid_criticality", "detail": crit})
    pinned = str(fm.get("pinned", "")).strip().lower() in ("true", "yes", "1")
    if pinned and crit and crit != "load-bearing":
        findings.append({"path": str(path), "issue": "pin_crit_mismatch",
                         "detail": f"pinned={pinned} crit={crit}"})

    created = parse_iso(fm.get("created", ""))
    if created and created > now() + datetime.timedelta(days=1):
        findings.append({"path": str(path), "issue": "future_created", "detail": str(created)})

    ttl_raw = (fm.get("ttl_days") or "").strip()
    last_v = parse_iso(fm.get("last_verified", "")) or created
    if crit == "ephemeral" and ttl_raw and last_v:
        try:
            ttl = int(ttl_raw)
            if ttl > 0:
                age_days = (now() - last_v).days
                if age_days > ttl + cfg.grace_days:
                    findings.append({"path": str(path), "issue": "expired_ttl",
                                     "detail": f"age={age_days}d ttl={ttl}d grace={cfg.grace_days}"})
        except ValueError:
            findings.append({"path": str(path), "issue": "invalid_ttl_days", "detail": ttl_raw})

    return findings


def patch_missing_created(path: Path) -> bool:
    """Stamp `created` from the file mtime if it is missing. True if patched."""
    text = path.read_text(errors="replace")
    fm, body = parse_frontmatter(text)
    if not fm or "created" in fm:
        return False
    fm["created"] = file_mtime(path).isoformat(timespec="seconds")
    path.write_text(serialize_frontmatter(fm, body))
    return True


def run_audit(cfg: Config, roots: list[Path] | None = None, patch: bool = False,
              apply: bool = False, limit: int = 0) -> dict:
    roots = roots if roots is not None else cfg.crawl_roots()
    findings: list[dict] = []
    scanned = patched = 0
    for root in roots:
        for path in iter_md_files(cfg, root):
            if limit and scanned >= limit:
                break
            scanned += 1
            fs = lint_one(cfg, path)
            findings.extend(fs)
            if patch and any(f["issue"] == "missing_required" and f["detail"] == "created" for f in fs):
                if cfg.is_read_only(path):
                    continue
                if not apply or patch_missing_created(path):
                    patched += 1
    by_root: dict = defaultdict(int)
    for f in findings:
        for r in roots:
            if f["path"].startswith(str(r)):
                by_root[str(r)] += 1
                break
    return {
        "files_scanned": scanned,
        "findings_total": len(findings),
        "by_issue": dict(Counter(f["issue"] for f in findings)),
        "by_root": dict(by_root),
        "files_patched": patched,
        "patch_applied": bool(patch and apply),
        "findings": findings,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="arcturion_memory audit", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="config JSON (else $ARC_MEMORY_CONFIG)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--root", help="restrict to one folder")
    ap.add_argument("--patch", action="store_true", help="stamp missing `created` (preview without --apply)")
    ap.add_argument("--apply", action="store_true", help="required with --patch to write")
    ap.add_argument("--limit", type=int, default=0, help="cap files scanned (0 = all)")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    roots = [Path(args.root).expanduser().resolve()] if args.root else None
    res = run_audit(cfg, roots, patch=args.patch, apply=args.apply, limit=args.limit)

    if args.json:
        print(json.dumps(res, indent=2, default=str))
        return 0
    print(f"Memory audit: scanned {res['files_scanned']} files, {res['findings_total']} findings\n")
    print("By issue:")
    for issue, n in sorted(res["by_issue"].items(), key=lambda kv: -kv[1]):
        print(f"  {issue:24s} {n}")
    if res["by_root"]:
        print("\nBy root:")
        for r, n in sorted(res["by_root"].items(), key=lambda x: -x[1])[:10]:
            print(f"  {n:5d}  {r}")
    if args.patch:
        verb = "patched" if args.apply else "would patch"
        print(f"\n{verb}: {res['files_patched']} files (missing `created` -> file mtime)")
        if not args.apply and res["files_patched"]:
            print("   re-run with --patch --apply to write")
    else:
        print("\nFirst 20 findings:")
        for f in res["findings"][:20]:
            print(f"  {f['issue']:24s} {f['path'][-90:]}  ({f['detail'][:40]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
