# Example memory (synthetic)

Two invented agents, `builder` and `researcher`, with a handful of memory files.
Nothing here describes a real person or project. The data is seeded with
problems on purpose:

- `builder/Memory/MEMORY.md` points at a file that doesn't exist and misses
  most of its siblings (health grade drops; `index` repairs it)
- `builder/Memory/Preferences/` holds a near-duplicate pair (`dedup` finds it)
- `builder/Memory/scratch-note.md` has a future date, an unknown type, an
  unknown owner, a bad criticality and a malformed line (`audit` reports them)
