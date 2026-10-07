"""python3 -m arcturion_memory <command> [options]

Commands:
  audit    lint frontmatter across every memory folder
  dedup    report near-duplicate memory files
  health   score each agent's MEMORY.md index (A-F)
  index    regenerate MEMORY.md hubs without flattening curation
  recall   keyword search across memory, fused across signals
"""
from __future__ import annotations

import sys

from . import audit, dedup, health, indexer, recall

COMMANDS = {"audit": audit.main, "dedup": dedup.main, "health": health.main,
            "index": indexer.main, "recall": recall.main}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help") or argv[0] not in COMMANDS:
        print(__doc__)
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    return COMMANDS[argv[0]](argv[1:]) or 0


if __name__ == "__main__":
    raise SystemExit(main())
