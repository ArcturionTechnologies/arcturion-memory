# ArcturionMemory

Audit, dedupe, health-check, index and search AI-agent memory files.

Agents that keep long-term memory as markdown files (one note per fact, with a
`MEMORY.md` hub that indexes them) slowly accumulate problems: notes without
dates, copies of the same rule in two places, a hub that points at files that
were renamed, and no good way to find "that thing we decided in March".
ArcturionMemory is five small command-line tools for keeping that kind of memory
healthy:

| Command | What it does | Writes? |
| --- | --- | --- |
| `audit` | Lints frontmatter: missing keys, bad types, unknown owners, future dates, expired TTLs | Only with `--patch --apply` |
| `dedup` | Finds near-duplicate notes and suggests which copy to keep | Never |
| `health` | Grades each agent's `MEMORY.md` hub A–F on six criteria | Never |
| `index` | Regenerates `MEMORY.md` hubs without flattening hand-made structure | Yes (backs up first) |
| `recall` | Keyword search across memory, ranked by fusing two signals | Never |

Python 3.10+, standard library only. `dedup` can optionally use
`sentence-transformers` for semantic similarity.

> **Portfolio project.** This is an open-source sample of the tooling behind
> Arcturion's multi-agent setup. It is not a commercial product and makes no
> claims about revenue or customers. All bundled data is synthetic.

## Quickstart

```bash
git clone https://github.com/ArcturionTechnologies/arcturion-memory.git
cd arcturion-memory
CONF=examples/config.example.json

python3 -m arcturion_memory health --config $CONF    # builder scores B: its hub is stale
python3 -m arcturion_memory audit  --config $CONF    # 5 problems in one seeded scratch note
python3 -m arcturion_memory dedup  --config $CONF --embedder lexical --min-chars 100   # 1 duplicate pair
python3 -m arcturion_memory recall "sample size" --config $CONF

python3 -m arcturion_memory index  --config $CONF --dry-run   # preview
python3 -m arcturion_memory index  --config $CONF             # rewrite hubs (old one backed up)
python3 -m arcturion_memory health --config $CONF             # both agents now grade A
```

`pip install .` adds an `arcturion-memory` command that does the same thing.

## Memory layout it expects

```
<agents_root>/
  builder/
    Memory/
      MEMORY.md                 the hub (index)
      home.md                   top-level notes
      Preferences/*.md          category folders: People, Preferences, Decisions, Pointers, Sessions, ...
      Decisions/INDEX.md        big folders can keep their own index
  researcher/
    Memory/...
```

Each note is markdown with simple `key: value` frontmatter. The keys the tools
understand: `name`, `description`, `type`, `created`, `last_verified`,
`ttl_days`, `criticality` (`load-bearing` / `reference` / `ephemeral`),
`owner_agent`, `pinned`, plus the dedup markers below.

If memory sits one level deeper (for example `<agent>/000 🤖 Builder/Memory/`),
set `"inner_glob": "000 *"`. When an agent has several matching folders, the one
that actually holds a `Boot/` or `Memory/` folder wins over an empty look-alike.

## Configuration

One JSON file, passed with `--config` or set in `ARC_MEMORY_CONFIG`. Without one,
every sub-folder of `$ARC_ROOT` (or the current folder) is treated as an agent.
Relative paths resolve against the config file's folder. See
[`examples/config.example.json`](examples/config.example.json).

| Key | Meaning |
| --- | --- |
| `agents_root`, `agents` | Where agents live, and which ones (omit `agents` to discover them) |
| `inner_glob`, `memory_dirname` | Optional nested home folder; memory folder name (default `Memory`) |
| `extra_crawl_roots` | More folders for `audit` and `dedup` to scan |
| `harness_globs` | Extra memory folders (for example per-project harness memory) used with `--include-harness` |
| `read_only_prefixes` | `audit --patch` never writes under these |
| `dedup_skip_prefixes` | Folders `dedup` ignores (templated files, known mirrors) |
| `load_bearing_filenames`, `load_bearing_path_regex`, `load_bearing_prefixes` | What the indexer pins to the top as load-bearing |
| `ttl_by_type`, `grace_days` | TTLs by `type` for the expired-TTL check |
| `extra_owner_agents` | Owner values accepted besides the agent names (default `shared`, `unknown`) |
| `domain_owners` | Keyword rules (`pattern`, `owner`, `also`) that `dedup` uses to suggest an owner |
| `recall_index_files`, `recall_roots` | What `recall` searches (default: every agent's memory folder) |

## How each tool works

**health** scores each hub out of 100: completeness (every sibling note is
listed, 20), currency (the hub isn't older than its newest note, 20), sortedness
(pinned and load-bearing entries first, 15), hook quality (every entry has a
description under 150 characters, 15), frontmatter alignment (entry names match
each note's `name`, 15), conciseness (under about 200 lines and 25 KB, 15). Links
into category folders count as valid; they aren't flagged as broken.

**index** rewrites each hub but keeps its frontmatter, title and intro text
verbatim. It writes a pinned / load-bearing section first, then a Root section,
then one section per category folder. A folder with more than 40 notes, or one
that already has an `INDEX.md`, gets a single pointer line instead. An existing
`INDEX.md` is never overwritten. The old hub is saved as
`MEMORY.md.bak-curation-<date>`. Only files that exist are listed.

**dedup** compares note bodies. `--embedder lexical` uses hashed word counts
(standard library, catches copies and light edits). `--embedder minilm` uses
`sentence-transformers` (`pip install .[semantic]`, catches paraphrases). The
default `auto` uses minilm when it's installed and lexical otherwise, and the
report says which one ran. Notes marked `is_mirror: true`, `mirrored_from: …`,
or `dedup_exempt: true` with a `dedup_exempt_reason` are skipped. An exemption
without a reason is ignored on purpose.

**audit** reports problems and changes nothing by default. `--patch` previews
stamping a missing `created` from the file's modified time; `--patch --apply`
writes it, except under `read_only_prefixes`.

**recall** searches the configured index files and the full text of every note,
then merges the two ranked lists with reciprocal rank fusion, so a note that
ranks well on both comes first. A hit on an index line counts for the note that
line links to.

## Project layout

```
arcturion_memory/
  config.py      config loading, agent discovery, path rules
  common.py      frontmatter + time helpers
  audit.py  dedup.py  health.py  indexer.py  recall.py
  __main__.py    `python3 -m arcturion_memory <command>`
examples/        synthetic two-agent memory + config
tests/           45 tests, stdlib unittest
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

All tests build their own files in temporary folders. No network, and `dedup`
tests use the lexical embedder so no model is downloaded.

## License

MIT. See [LICENSE](LICENSE).

Implementation is AI-assisted; architecture, requirements, and testing directed by Robert Lingoes.
