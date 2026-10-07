#!/usr/bin/env python3
"""Characterization tests for the curation-aware indexer (stdlib only)."""
from __future__ import annotations
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from arcturion_memory import indexer as mi  # noqa: E402
from arcturion_memory.config import Config  # noqa: E402


def write(p: Path, fm: dict | None = None, body: str = "") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    text = ""
    if fm is not None:
        text += "---\n" + "\n".join(f"{k}: {v}" for k, v in fm.items()) + "\n---\n"
    text += body
    p.write_text(text)


class IndexerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # canonical shape: <root>/000 🤖 Test/Memory
        self.mem = Path(self.tmp.name) / "000 🤖 Test" / "Memory"
        self.mem.mkdir(parents=True)
        self.cfg = Config(agents_root=Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def _regen(self):
        mi.regen_index(self.cfg, self.mem)
        return (self.mem / "MEMORY.md").read_text()

    def test_home_md_is_indexed(self):
        write(self.mem / "🏠 home.md", {"name": "Home"}, "# Home\nmap")
        out = self._regen()
        self.assertIn("(🏠 home.md)", out)

    def test_frontmatter_preserved_verbatim_nested(self):
        # nested metadata: block must survive untouched (the old serializer flattened it)
        hub = self.mem / "MEMORY.md"
        hub.write_text('---\nname: ""\nmetadata:\n  type: index\n  owner: TEST\n---\n\n'
                       "# 🧠 Test — Memory Index\n\n> blurb line\n\n## 📂 Root\n- [old](old.md)\n")
        write(self.mem / "real.md", {"name": "Real"}, "# Real")
        out = self._regen()
        self.assertIn("metadata:\n  type: index\n  owner: TEST", out)
        self.assertIn("> blurb line", out)
        self.assertIn("(real.md)", out)
        self.assertNotIn("(old.md)", out)  # stale entry dropped

    def test_pinned_hoisted_before_plain(self):
        write(self.mem / "🏠 home.md", {"name": "Home"}, "x")
        write(self.mem / "Preferences" / "p1.md", {"name": "Pref One"}, "x")
        write(self.mem / "Preferences" / "pinned.md",
              {"name": "Critical", "pinned": "true"}, "x")
        out = self._regen()
        self.assertIn("## 📌 Pinned & Load-bearing", out)
        pin_pos = out.index("📌 [Critical]")
        # the pinned entry must precede the plain Preferences entry
        self.assertLess(pin_pos, out.index("[Pref One]"))
        # and precede the Root section's plain home entry
        self.assertLess(pin_pos, out.index("[Home]"))

    def test_large_subdir_uses_pointer_and_generates_index(self):
        for i in range(45):
            write(self.mem / "Decisions" / f"d{i:02d}.md", {"name": f"D{i}"}, "x")
        out = self._regen()
        self.assertIn("[Decisions/INDEX.md](Decisions/INDEX.md)", out)
        self.assertTrue((self.mem / "Decisions" / "INDEX.md").exists())
        # the 45 entries are NOT inlined into the hub
        self.assertNotIn("(Decisions/d01.md)", out)

    def test_small_subdir_inlined(self):
        write(self.mem / "People" / "a.md", {"name": "Alice"}, "x")
        write(self.mem / "People" / "b.md", {"name": "Bob"}, "x")
        out = self._regen()
        self.assertIn("## 🧑 People", out)
        self.assertIn("(People/a.md)", out)
        self.assertIn("(People/b.md)", out)

    def test_existing_index_never_overwritten(self):
        sub = self.mem / "Pointers"
        write(sub / "p.md", {"name": "P"}, "x")
        idx = sub / "INDEX.md"
        idx.write_text("# HAND CURATED — do not touch\n- [P](p.md)\n")
        self._regen()
        self.assertIn("HAND CURATED", idx.read_text())

    def test_no_fabrication(self):
        write(self.mem / "🏠 home.md", {"name": "Home"}, "x")
        write(self.mem / "one.md", {"name": "One"}, "x")
        out = self._regen()
        hrefs = [ln.split("](", 1)[1].split(")", 1)[0]
                 for ln in out.splitlines() if ln.strip().startswith("- ") and "](" in ln]
        for h in hrefs:
            if h.endswith("/INDEX.md"):
                continue
            self.assertTrue((self.mem / h).exists(), f"index points to missing file: {h}")

    def test_backup_written(self):
        hub = self.mem / "MEMORY.md"
        hub.write_text("# old hub\n- [x](x.md)\n")
        write(self.mem / "y.md", {"name": "Y"}, "x")
        mi.regen_index(self.cfg, self.mem)
        backups = list(self.mem.glob("MEMORY.md.bak-curation-*"))
        self.assertTrue(backups, "expected a backup file")
        self.assertIn("old hub", backups[0].read_text())

    def test_flat_layout_title_uses_agent_folder_name(self):
        mem = Path(self.tmp.name) / "builder" / "Memory"
        write(mem / "a.md", {"name": "A"}, "x")
        mi.regen_index(self.cfg, mem)
        self.assertIn("# 🧠 builder — Memory Index", (mem / "MEMORY.md").read_text())

    def test_dry_run_writes_nothing(self):
        write(self.mem / "a.md", {"name": "A"}, "x")
        report = mi.regen_index(self.cfg, self.mem, dry=True)
        self.assertTrue(report["dry"])
        self.assertFalse((self.mem / "MEMORY.md").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
