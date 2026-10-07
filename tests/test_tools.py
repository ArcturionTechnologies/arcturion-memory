#!/usr/bin/env python3
"""Tests for config, audit, dedup, health and recall (stdlib only, no network)."""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from arcturion_memory import __main__ as cli  # noqa: E402
from arcturion_memory import audit, dedup, health, indexer, recall  # noqa: E402
from arcturion_memory.config import Config, load_config  # noqa: E402

LONG = ("Run the full test suite and paste the pass and fail counts before reporting "
        "that a change is finished. A skipped run once hid a broken import for a day. ")


def write(p: Path, fm: dict | None = None, body: str = "") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    text = ""
    if fm is not None:
        text += "---\n" + "\n".join(f"{k}: {v}" for k, v in fm.items()) + "\n---\n"
    p.write_text(text + body)
    return p


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg = Config(agents_root=self.root, agents=["builder", "researcher"])
        self.b = self.root / "builder" / "Memory"
        self.r = self.root / "researcher" / "Memory"
        self.b.mkdir(parents=True)
        self.r.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()


# -- config --------------------------------------------------------------------
class ConfigTests(TmpCase):
    def test_discovers_agents_when_list_omitted(self):
        (self.root / ".hidden").mkdir()
        (self.root / "_scratch").mkdir()
        cfg = Config(agents_root=self.root)
        self.assertEqual(cfg.agent_names(), ["builder", "researcher"])

    def test_inner_glob_prefers_populated_home_over_lookalike(self):
        home = self.root / "web"
        (home / "000 Web").mkdir(parents=True)           # sorts first, empty
        (home / "000 🤖 Web" / "Memory").mkdir(parents=True)
        cfg = Config(agents_root=self.root, agents=["web"], inner_glob="000 *")
        self.assertEqual(cfg.agent_memory_dir("web"), home / "000 🤖 Web" / "Memory")

    def test_load_config_resolves_relative_paths_against_file(self):
        cfg_file = self.root / "conf" / "memory.json"
        cfg_file.parent.mkdir()
        cfg_file.write_text(json.dumps({
            "agents_root": "..", "agents": ["builder"],
            "read_only_prefixes": ["../builder/Memory/locked"],
            "ttl_by_type": {"scratch": 3},
        }))
        cfg = load_config(cfg_file)
        self.assertEqual(cfg.agents_root, self.root.resolve())
        self.assertEqual(cfg.read_only_prefixes, [str((self.root.resolve() / "conf" / ".." / "builder/Memory/locked"))])
        self.assertEqual(cfg.ttl_by_type["scratch"], 3)
        self.assertEqual(cfg.ttl_by_type["feedback"], 365)  # defaults kept

    def test_env_config_and_arc_root_fallback(self):
        with mock.patch.dict(os.environ, {"ARC_ROOT": str(self.root)}, clear=False):
            os.environ.pop("ARC_MEMORY_CONFIG", None)
            cfg = load_config(None)
        self.assertEqual(cfg.agents_root, self.root.resolve())

    def test_harness_globs(self):
        (self.root / "harness" / "p1" / "memory").mkdir(parents=True)
        (self.root / "harness" / "p2" / "memory").mkdir(parents=True)
        cfg = Config(agents_root=self.root, harness_globs=[str(self.root / "harness" / "*" / "memory")])
        self.assertEqual(len(cfg.harness_dirs()), 2)

    def test_load_bearing_rules(self):
        self.assertTrue(self.cfg.is_load_bearing(Path("feedback_x.md"))[0])
        self.assertTrue(self.cfg.is_load_bearing(Path("deploy-runbook.md"))[0])
        self.assertFalse(self.cfg.is_load_bearing(Path("notes.md"))[0])


# -- audit ---------------------------------------------------------------------
class AuditTests(TmpCase):
    def test_clean_file_has_no_findings(self):
        f = write(self.b / "ok.md", {"name": "Ok", "type": "feedback", "created": "2026-01-01",
                                     "owner_agent": "builder"}, "body")
        self.assertEqual(audit.lint_one(self.cfg, f), [])

    def test_reports_each_problem_class(self):
        f = write(self.b / "bad.md", {"name": "Bad", "type": "nonsense", "created": "2099-01-01",
                                      "owner_agent": "stranger", "criticality": "urgent"}, "x")
        f.write_text(f.read_text().replace("---\nname", "---\nno colon here\nname", 1))
        issues = {x["issue"] for x in audit.lint_one(self.cfg, f)}
        self.assertEqual(issues, {"invalid_type", "future_created", "invalid_owner_agent",
                                  "invalid_criticality", "malformed_yaml_line"})

    def test_missing_required_and_pin_mismatch(self):
        f = write(self.b / "p.md", {"name": "P", "pinned": "true", "criticality": "reference"}, "x")
        found = {(x["issue"], x["detail"]) for x in audit.lint_one(self.cfg, f)}
        self.assertIn(("missing_required", "type"), found)
        self.assertIn(("missing_required", "created"), found)
        self.assertIn("pin_crit_mismatch", {i for i, _ in found})

    def test_expired_ttl_only_for_ephemeral(self):
        old = {"name": "E", "type": "scan", "created": "2020-01-01", "ttl_days": "14"}
        eph = write(self.b / "e.md", {**old, "criticality": "ephemeral"}, "x")
        ref = write(self.b / "r.md", {**old, "criticality": "reference"}, "x")
        self.assertIn("expired_ttl", {x["issue"] for x in audit.lint_one(self.cfg, eph)})
        self.assertNotIn("expired_ttl", {x["issue"] for x in audit.lint_one(self.cfg, ref)})

    def test_files_without_frontmatter_and_index_files_are_ignored(self):
        write(self.b / "plain.md", None, "no frontmatter")
        write(self.b / "MEMORY.md", {"name": "idx"}, "x")
        res = audit.run_audit(self.cfg)
        self.assertEqual(res["findings_total"], 0)
        self.assertEqual(res["files_scanned"], 1)  # MEMORY.md is not scanned

    def test_patch_previews_then_applies_and_respects_read_only(self):
        f = write(self.b / "nocreated.md", {"name": "N", "type": "feedback"}, "x")
        locked = write(self.r / "locked.md", {"name": "L", "type": "feedback"}, "x")
        self.cfg.read_only_prefixes = [str(self.r)]
        preview = audit.run_audit(self.cfg, patch=True, apply=False)
        self.assertEqual(preview["files_patched"], 1)
        self.assertNotIn("created:", f.read_text())
        applied = audit.run_audit(self.cfg, patch=True, apply=True)
        self.assertEqual(applied["files_patched"], 1)
        self.assertIn("created:", f.read_text())
        self.assertNotIn("created:", locked.read_text())

    def test_cli_json(self):
        write(self.b / "bad.md", {"name": "B", "type": "nonsense", "created": "2026-01-01"}, "x")
        cfg_file = self.root / "c.json"
        cfg_file.write_text(json.dumps({"agents_root": str(self.root), "agents": ["builder"]}))
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = audit.main(["--config", str(cfg_file), "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue())["by_issue"], {"invalid_type": 1})


# -- dedup ---------------------------------------------------------------------
class DedupTests(TmpCase):
    def test_lexical_embedding_is_unit_length_and_deterministic(self):
        a, b = dedup.lexical_embed(["alpha beta beta"]), dedup.lexical_embed(["alpha beta beta"])
        self.assertEqual(a, b)
        self.assertAlmostEqual(sum(x * x for x in a[0]), 1.0, places=6)

    def test_finds_near_duplicates_and_picks_primary(self):
        write(self.b / "a.md", {"name": "A", "criticality": "load-bearing", "owner_agent": "builder"}, LONG + "Extra.")
        write(self.b / "b.md", {"name": "B", "owner_agent": "builder"}, LONG)
        write(self.b / "c.md", {"name": "C"}, "Completely different topic about gardening tomatoes " * 6)
        res = dedup.find_duplicates(self.cfg, threshold=0.9, min_chars=50, embedder="lexical")
        self.assertEqual(res["embedder"], "lexical")
        self.assertEqual(len(res["clusters"]), 1)
        c = res["clusters"][0]
        self.assertTrue(c["primary"].endswith("a.md"))   # load-bearing wins
        self.assertTrue(c["candidates"][0].endswith("b.md"))
        self.assertGreaterEqual(c["min_similarity"], 0.9)

    def test_mirrors_exemptions_and_short_files_are_skipped(self):
        write(self.b / "a.md", {"name": "A"}, LONG)
        write(self.b / "m.md", {"name": "M", "is_mirror": "true"}, LONG)
        write(self.b / "f.md", {"name": "F", "mirrored_from": "a.md"}, LONG)
        write(self.b / "x.md", {"name": "X", "dedup_exempt": "true", "dedup_exempt_reason": "reviewed"}, LONG)
        write(self.b / "tiny.md", {"name": "T"}, "short")
        files = [p.name for p, _, _ in dedup.iter_files(self.cfg, self.cfg.crawl_roots(), min_chars=50)]
        self.assertEqual(files, ["a.md"])

    def test_exemption_without_reason_is_not_honored(self):
        write(self.b / "x.md", {"name": "X", "dedup_exempt": "true"}, LONG)
        files = [p.name for p, _, _ in dedup.iter_files(self.cfg, self.cfg.crawl_roots(), min_chars=50)]
        self.assertEqual(files, ["x.md"])

    def test_dedup_skip_prefixes(self):
        write(self.b / "skip" / "a.md", {"name": "A"}, LONG)
        self.cfg.dedup_skip_prefixes = [str(self.b / "skip")]
        self.assertEqual(list(dedup.iter_files(self.cfg, self.cfg.crawl_roots(), 50)), [])

    def test_owner_reassignment_recommendation_from_domain_rules(self):
        self.cfg.domain_owners = [{"pattern": r"\bcitation\b", "owner": "researcher"}]
        body = "Every citation must link the primary paper and the dataset it used. " * 5
        write(self.b / "a.md", {"name": "A", "owner_agent": "builder"}, body + "more")
        write(self.b / "b.md", {"name": "B", "owner_agent": "builder"}, body)
        c = dedup.find_duplicates(self.cfg, min_chars=50, embedder="lexical")["clusters"][0]
        self.assertEqual(c["suggested_owners"], ["researcher"])
        self.assertEqual(c["recommendation"], "reassign primary to researcher")

    def test_custom_embedder_and_too_few_files(self):
        write(self.b / "a.md", {"name": "A"}, LONG)
        res = dedup.find_duplicates(self.cfg, min_chars=50, embedder=lambda texts: [[1.0]] * len(texts))
        self.assertEqual(res["clusters"], [])
        self.assertEqual(res["total_files"], 1)

    def test_minilm_missing_is_reported(self):
        with mock.patch.object(dedup, "minilm_embedder", side_effect=ImportError("nope")):
            self.assertEqual(dedup.pick_embedder("auto")[0], "lexical")
            with self.assertRaises(ImportError):
                dedup.pick_embedder("minilm")


# -- health --------------------------------------------------------------------
class HealthTests(TmpCase):
    def _good_index(self):
        write(self.b / "one.md", {"name": "One"}, "x")
        write(self.b / "People" / "p.md", {"name": "Pat"}, "x")
        (self.b / "MEMORY.md").write_text(
            "# idx\n\n- 📌 [Pat](People/p.md) — a person\n- [One](one.md) — first note\n")

    def test_missing_index_is_f(self):
        r = health.score_one(self.cfg, self.b)
        self.assertEqual((r["grade"], r["score"]), ("F", 0))
        self.assertIn("python3 -m arcturion_memory index", r["suggestions"][0])

    def test_complete_index_scores_a_and_subdir_links_are_not_broken(self):
        self._good_index()
        r = health.score_one(self.cfg, self.b)
        self.assertEqual(r["grade"], "A")
        self.assertEqual(r["score"], 100)
        self.assertFalse(any("missing files" in i for i in r["issues"]))

    def test_broken_and_missing_entries_lower_completeness(self):
        write(self.b / "one.md", {"name": "One"}, "x")
        write(self.b / "two.md", {"name": "Two"}, "x")
        (self.b / "MEMORY.md").write_text("- [One](one.md) — first\n- [Gone](gone.md) — deleted\n")
        r = health.score_one(self.cfg, self.b)
        self.assertEqual(r["scores"]["completeness"], 10)
        self.assertTrue(any("point to missing files" in i for i in r["issues"]))

    def test_stale_index_loses_currency(self):
        self._good_index()
        idx = self.b / "MEMORY.md"
        old = time.time() - 20 * 86400
        os.utime(idx, (old, old))
        r = health.score_one(self.cfg, self.b)
        self.assertEqual(r["scores"]["currency"], 5)

    def test_hooks_alignment_and_sortedness(self):
        write(self.b / "one.md", {"name": "Real Name"}, "x")
        write(self.b / "two.md", {"name": "Two"}, "x")
        (self.b / "MEMORY.md").write_text("- [Wrong](one.md)\n- 📌 [Two](two.md) — " + "h" * 200 + "\n")
        r = health.score_one(self.cfg, self.b)
        self.assertLess(r["scores"]["hook_quality"], 15)
        self.assertLess(r["scores"]["alignment"], 15)
        self.assertLess(r["scores"]["sortedness"], 15)

    def test_wikilinks_parse(self):
        e = health.parse_entry("- [[some_note]] — hook")
        self.assertEqual((e["href"], e["hook"]), ("some_note.md", "hook"))

    def test_indexer_output_scores_a(self):
        write(self.b / "home.md", {"name": "Home", "description": "map"}, "x")
        write(self.b / "Decisions" / "d.md", {"name": "D", "description": "a decision"}, "x")
        indexer.regen_index(self.cfg, self.b)
        self.assertEqual(health.score_one(self.cfg, self.b)["grade"], "A")

    def test_cli_agent_filter(self):
        self._good_index()
        cfg_file = self.root / "c.json"
        cfg_file.write_text(json.dumps({"agents_root": str(self.root), "agents": ["builder", "researcher"]}))
        buf = io.StringIO()
        with redirect_stdout(buf):
            health.main(["--config", str(cfg_file), "--agent", "builder", "--json"])
        reports = json.loads(buf.getvalue())
        self.assertEqual([r["agent"] for r in reports], ["builder"])


# -- recall --------------------------------------------------------------------
class RecallTests(TmpCase):
    def test_rrf_rewards_agreement(self):
        a, b, c = Path("a"), Path("b"), Path("c")
        fused = recall.reciprocal_rank_fusion([[a, b], [b, c]])
        self.assertEqual(fused[0][0], b)

    def test_full_text_and_index_signals_fuse(self):
        note = write(self.b / "Preferences" / "tests.md",
                     {"name": "Tests first", "owner_agent": "builder", "last_verified": "2026-03-01"},
                     "Paste the test counts before saying done.")
        write(self.r / "other.md", {"name": "Other"}, "nothing relevant")
        idx = write(self.b / "MEMORY.md", None, "- [Tests first](Preferences/tests.md) — test counts rule\n")
        self.cfg.recall_index_files = [idx]
        res = recall.recall(self.cfg, "test counts")
        self.assertEqual(res["index_hits"], 1)
        self.assertEqual(res["text_hits"], 2)  # the note and the index line itself
        top = res["results"][0]
        self.assertEqual(Path(top["path"]), note.resolve())
        self.assertEqual((top["owner"], top["verified"]), ("builder", "2026-03-01"))

    def test_no_hits(self):
        self.assertEqual(recall.recall(self.cfg, "zzz-not-there")["results"], [])

    def test_grep_caps_hits_per_file_and_skips_vendor_dirs(self):
        write(self.b / "many.md", None, "\n".join(["needle"] * 20))
        write(self.b / "node_modules" / "x.md", None, "needle")
        hits = recall.grep("needle", [self.b], per_file=5)
        self.assertEqual(len(hits), 5)


# -- command dispatcher ----------------------------------------------------------
class DispatchTests(unittest.TestCase):
    def test_unknown_command_returns_2(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["nope"]), 2)
            self.assertEqual(cli.main(["--help"]), 0)

    def test_bundled_example_runs_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            ex = Path(tmp) / "examples"
            shutil.copytree(REPO / "examples", ex)
            conf = str(ex / "config.example.json")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["health", "--config", conf]), 0)
                self.assertEqual(cli.main(["index", "--config", conf]), 0)
            cfg = load_config(conf)
            grades = {name: health.score_one(cfg, d)["grade"] for name, d in cfg.memory_dirs()}
            self.assertEqual(grades, {"builder": "A", "researcher": "A"})
            res = dedup.find_duplicates(cfg, min_chars=100, embedder="lexical")
            self.assertEqual(len(res["clusters"]), 1)
            self.assertEqual(audit.run_audit(cfg)["findings_total"], 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
