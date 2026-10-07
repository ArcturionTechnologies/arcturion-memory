"""dedup: find near-duplicate memory files. Report only, never writes.

  1. Crawl every root (skipping skip_parts, dedup_skip_prefixes and index files)
  2. Embed each body (after frontmatter)
  3. Cosine similarity >= threshold -> candidate cluster
  4. Report clusters with paths, sizes and frontmatter for a human to merge

Embedders:
  minilm   sentence-transformers all-MiniLM-L6-v2 (semantic; optional install)
  lexical  hashed bag-of-words, standard library only (catches copy-paste and
           light edits, not paraphrase)
  auto     minilm when installed, otherwise lexical (the default)

Mark legitimate copies so later runs skip them:
  is_mirror: true            symmetrical mirror, no primary
  mirrored_from: <path>      one-way mirror with provenance
  dedup_exempt: true         reviewed false positive; requires dedup_exempt_reason

  python3 -m arcturion_memory dedup [--config FILE] [--threshold 0.9] [--json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Callable, Sequence

from .common import iter_md_files, parse_frontmatter
from .config import Config, load_config

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")  # macOS OpenMP conflict guard

MINILM = "sentence-transformers/all-MiniLM-L6-v2"
LEXICAL_DIMS = 2048
Embedder = Callable[[Sequence[str]], list[list[float]]]


# -- embedders ---------------------------------------------------------------
def lexical_embed(texts: Sequence[str], dims: int = LEXICAL_DIMS) -> list[list[float]]:
    """Unit-length hashed term-frequency vectors (stdlib only, deterministic)."""
    out = []
    for text in texts:
        vec = [0.0] * dims
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "big")
            vec[h % dims] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        out.append([v / norm for v in vec])
    return out


def minilm_embedder() -> Embedder:
    from sentence_transformers import SentenceTransformer  # optional dependency
    model = SentenceTransformer(MINILM)

    def embed(texts: Sequence[str]) -> list[list[float]]:
        vecs = model.encode(list(texts), show_progress_bar=False, normalize_embeddings=True)
        rows = [[float(x) for x in row] for row in vecs]
        if not all(math.isfinite(x) for row in rows for x in row):
            raise ValueError("embedding model returned non-finite vectors; result would be invalid")
        return rows

    return embed


def pick_embedder(name: str) -> tuple[str, Embedder]:
    if name == "lexical":
        return "lexical", lexical_embed
    if name in ("minilm", "auto"):
        try:
            return "minilm", minilm_embedder()
        except ImportError:
            if name == "minilm":
                raise
            return "lexical", lexical_embed
    raise ValueError(f"unknown embedder {name!r}")


# -- collection --------------------------------------------------------------
def _raw_true(text: str, key: str) -> bool:
    return bool(re.search(rf"(?mi)^{re.escape(key)}:\s*['\"]?(true|yes|1)['\"]?\s*$", text))


def _raw_value(text: str, key: str) -> str:
    m = re.search(rf"(?mi)^{re.escape(key)}:\s*(.+?)\s*$", text)
    return m.group(1).strip() if m else ""


def iter_files(cfg: Config, roots: list[Path], min_chars: int):
    for root in roots:
        for p in iter_md_files(cfg, root):
            if cfg.is_dedup_skipped(p):
                continue
            try:
                text = p.read_text(errors="replace")
            except Exception:
                continue
            fm, body = parse_frontmatter(text)
            # Some legacy records carry a banner before their YAML block, so the
            # review markers are also honored from the raw text.
            if str(fm.get("is_mirror", "")).strip().lower() in ("true", "yes", "1") or _raw_true(text, "is_mirror"):
                continue
            if str(fm.get("mirrored_from", "")).strip() or _raw_value(text, "mirrored_from"):
                continue
            exempt_value = str(fm.get("dedup_exempt", "")).strip().strip("'\"").lower()
            exempt = exempt_value in ("true", "yes", "1") or _raw_true(text, "dedup_exempt")
            reason = str(fm.get("dedup_exempt_reason", "")).strip() or _raw_value(text, "dedup_exempt_reason")
            if exempt and reason:  # an exemption without a reason is ignored on purpose
                continue
            body = body.strip()
            if len(body) < min_chars:
                continue
            yield p, fm, body


# -- clustering --------------------------------------------------------------
def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))))


def cluster(vecs: list[list[float]], threshold: float) -> tuple[list[list[int]], Callable[[int, int], float]]:
    """Greedy single-pass clustering on cosine similarity (vectors are unit length)."""
    n = len(vecs)
    cache: dict[tuple[int, int], float] = {}

    def sim(i: int, j: int) -> float:
        key = (i, j) if i < j else (j, i)
        if key not in cache:
            cache[key] = _dot(vecs[i], vecs[j])
        return cache[key]

    clusters: list[list[int]] = []
    assigned: set[int] = set()
    for i in range(n):
        if i in assigned:
            continue
        group = [i]
        for j in range(i + 1, n):
            if j not in assigned and sim(i, j) >= threshold:
                group.append(j)
                assigned.add(j)
        if len(group) > 1:
            clusters.append(group)
            assigned.update(group)
    return clusters, sim


def detect_owners(cfg: Config, body: str) -> list[str]:
    """Owners suggested by config.domain_owners keyword rules."""
    owners: list[str] = []
    text = body.lower()[:3000]
    for rule in cfg.domain_owners:
        if re.search(rule.get("pattern", r"(?!)"), text):
            for a in [rule.get("owner", "")] + list(rule.get("also", [])):
                if a and a not in owners:
                    owners.append(a)
    return owners


CRIT_RANK = {"load-bearing": 0, "reference": 1, "ephemeral": 2}


def find_duplicates(cfg: Config, roots: list[Path] | None = None, threshold: float = 0.90,
                    min_chars: int = 200, limit: int = 0, embedder: str | Embedder = "auto") -> dict:
    roots = roots if roots is not None else cfg.crawl_roots()
    files = []
    for entry in iter_files(cfg, roots, min_chars):
        files.append(entry)
        if limit and len(files) >= limit:
            break
    if callable(embedder):
        embedder_name, embed = "custom", embedder
    else:
        embedder_name, embed = pick_embedder(embedder)
    result = {"clusters": [], "total_files": len(files), "threshold": threshold, "embedder": embedder_name}
    if len(files) < 2:
        return result

    vecs = embed([body[:2000] for _, _, body in files])  # first 2000 chars is usually enough
    groups, sim = cluster(vecs, threshold)
    for group in groups:
        members = []
        for idx in group:
            p, fm, body = files[idx]
            members.append({
                "path": str(p), "name": fm.get("name", p.stem), "type": fm.get("type", ""),
                "owner_agent": fm.get("owner_agent", ""), "chars": len(body),
                "criticality": fm.get("criticality", ""),
            })
        # primary = highest criticality, then longest
        members.sort(key=lambda m: (CRIT_RANK.get(m["criticality"], 9), -m["chars"]))
        suggested = detect_owners(cfg, files[group[0]][2])
        actual = sorted({m["owner_agent"].lower() for m in members if m["owner_agent"]})
        recommendation = "review"
        if suggested and len(actual) == 1 and suggested[0] != actual[0]:
            recommendation = f"reassign primary to {suggested[0]}"
        elif len(actual) > 1:
            recommendation = f"add `mirrored_from: {members[0]['path']}` to candidates (legitimate cross-agent mirrors)"
        elif len(members) == 2 and members[0]["chars"] > members[1]["chars"] * 1.5:
            recommendation = "merge candidate INTO primary, then archive candidate"
        result["clusters"].append({
            "primary": members[0]["path"],
            "candidates": [m["path"] for m in members[1:]],
            "members": members,
            "min_similarity": round(min(sim(i, j) for i in group for j in group if i != j), 3),
            "suggested_owners": suggested,
            "recommendation": recommendation,
        })
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="arcturion_memory dedup", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="config JSON (else $ARC_MEMORY_CONFIG)")
    ap.add_argument("--threshold", type=float, default=0.90, help="cosine similarity 0-1")
    ap.add_argument("--min-chars", type=int, default=200, help="skip bodies under N chars")
    ap.add_argument("--embedder", choices=["auto", "minilm", "lexical"], default="auto")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--root", help="restrict to one folder")
    ap.add_argument("--limit", type=int, default=0, help="cap files (0 = all)")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    roots = [Path(args.root).expanduser().resolve()] if args.root else None
    try:
        res = find_duplicates(cfg, roots, args.threshold, args.min_chars, args.limit, args.embedder)
    except ImportError as e:
        print(f"sentence-transformers not available: {e}\n"
              "  pip install sentence-transformers   (or use --embedder lexical)", file=sys.stderr)
        return 2
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(res, indent=2, default=str))
        return 0
    print(f"Memory dedup: {res['total_files']} files, {len(res['clusters'])} candidate clusters "
          f"@ threshold {res['threshold']} ({res['embedder']} embedder)\n")
    for i, c in enumerate(res["clusters"], 1):
        print(f"--- cluster {i} ({len(c['members'])} files, min sim {c['min_similarity']}) ---")
        for m in c["members"]:
            marker = "PRIMARY  " if m["path"] == c["primary"] else "candidate"
            print(f"  {marker}  {m['path'][-100:]}")
            print(f"             name={m['name'][:50]}  type={m['type']}  owner={m['owner_agent']}  "
                  f"chars={m['chars']}  crit={m['criticality']}")
        if c["suggested_owners"]:
            print(f"  suggested owners: {', '.join(c['suggested_owners'])}")
        print(f"  recommendation: {c['recommendation']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
