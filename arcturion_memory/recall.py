"""recall: keyword search over memory, fused across signals.

Signals (each produces a ranked list of files):
  1. index files   case-insensitive match inside the configured index files
                   (recall_index_files, for example a generated MEMORY.md or a
                   corpus-wide index); a hit counts for the file it links to
                   when the matching line holds a markdown link
  2. full text     case-insensitive match over every *.md under recall_roots
                   (default: every agent Memory folder)

The lists are merged with reciprocal rank fusion (score = sum 1 / (k + rank)),
so a file that ranks well on both signals rises to the top. Each result shows
the frontmatter's confidence, last_verified/last_modified and owner when present.

Pure Python, no ripgrep or vector store required.

  python3 -m arcturion_memory recall "query" [--config FILE] [--n 10] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .common import parse_frontmatter, short_path
from .config import Config, load_config

SKIP_PATH_PARTS = ("/node_modules/", "/.venv/", "/venv/", "/__pycache__/",
                   "/.git/", "/.next/", "/dist/", "/build/", "/.cache/",
                   "/site-packages/")
LINK_RE = re.compile(r"\]\(([^)]+\.md)\)")


def grep(query: str, paths: list[Path], n: int = 50, per_file: int = 5) -> list[tuple[Path, int, str]]:
    """Return (path, line_no, text) hits, at most per_file per file, n overall."""
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    hits: list[tuple[Path, int, str]] = []
    for root in paths:
        if not root.exists():
            continue
        files = [root] if root.is_file() else sorted(root.rglob("*.md"))
        for p in files:
            if any(x in str(p) for x in SKIP_PATH_PARTS):
                continue
            try:
                with p.open("r", encoding="utf-8", errors="ignore") as f:
                    count = 0
                    for i, line in enumerate(f, 1):
                        if pattern.search(line):
                            hits.append((p, i, line.strip()[:200]))
                            count += 1
                            if count >= per_file:
                                break
            except OSError:
                continue
            if len(hits) >= n:
                return hits
    return hits


def reciprocal_rank_fusion(rankings: list[list[Path]], k: int = 60) -> list[tuple[Path, float]]:
    """Standard RRF: score = sum(1 / (k + rank)) across rankings."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, p in enumerate(ranking):
            key = str(p)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
    return sorted(((Path(key), v) for key, v in scores.items()), key=lambda x: (-x[1], str(x[0])))


def _unique(paths: list[Path]) -> list[Path]:
    seen, out = set(), []
    for p in paths:
        if str(p) not in seen:
            seen.add(str(p))
            out.append(p)
    return out


def _index_targets(index_file: Path, hits: list[tuple[Path, int, str]]) -> list[Path]:
    """Resolve each index hit to the file its line links to (or the index itself)."""
    out = []
    for p, _ln, text in hits:
        m = LINK_RE.search(text)
        target = (index_file.parent / m.group(1)) if m else p
        out.append(target.resolve() if target.exists() else p)
    return out


def recall(cfg: Config, query: str, n: int = 10) -> dict:
    index_ranking: list[Path] = []
    for idx in cfg.recall_index_files:
        if idx.is_file():
            index_ranking += _index_targets(idx, grep(query, [idx], n=20, per_file=20))
    index_ranking = _unique(index_ranking)

    roots = cfg.recall_roots or [d for _, d in cfg.memory_dirs()]
    text_ranking = _unique([p.resolve() for p, _, _ in grep(query, roots, n=50)])

    fused = reciprocal_rank_fusion([index_ranking, text_ranking])[:n]
    results = []
    for p, score in fused:
        fm = {}
        if p.exists():
            try:
                fm, _ = parse_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                fm = {}
        results.append({
            "path": str(p), "score": round(score, 5),
            "name": fm.get("name", p.stem),
            "confidence": fm.get("confidence", ""),
            "verified": str(fm.get("last_verified", fm.get("last_modified", "")))[:10],
            "owner": fm.get("owner_agent", fm.get("owner", "")),
        })
    return {"query": query, "index_hits": len(index_ranking), "text_hits": len(text_ranking),
            "results": results}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="arcturion_memory recall", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query")
    ap.add_argument("--config", help="config JSON (else $ARC_MEMORY_CONFIG)")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    res = recall(cfg, args.query, args.n)
    if args.json:
        print(json.dumps(res, indent=2))
        return 0
    print(f"recall: {args.query!r}  (index hits {res['index_hits']}, full-text hits {res['text_hits']})\n")
    for i, r in enumerate(res["results"], 1):
        print(f"  {i:2}. [{r['score']:.4f}] {short_path(r['path'])}")
        print(f"      name={r['name']}  conf={r['confidence'] or '-'}  "
              f"verified={r['verified'] or '-'}  owner={r['owner'] or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
