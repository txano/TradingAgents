"""Tests for the automated self-improvement engine — focus on the prompt-edit
safety harness, weight clamping, trade consolidation, and the orchestrator."""

import json
import unittest
from pathlib import Path

import pytest

from tradingagents.learning import self_improve as si
from tradingagents.learning.self_improve import (
    apply_prompt_edits,
    apply_proposal,
    apply_weight_changes,
    run_self_improvement,
)
from tradingagents.learning.trade_reflections import consolidate_trades

# A real allowlisted path, created inside a temp repo for each test.
REL = "tradingagents/allocation/council.py"
REL2 = "tradingagents/agents/managers/portfolio_manager.py"


def _make_file(repo: Path, rel: str, content: str) -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


@pytest.mark.unit
class ApplyPromptEditsTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self.backups = self.repo / "_backups"

    def tearDown(self):
        self._tmp.cleanup()

    def test_applies_valid_unique_edit(self):
        p = _make_file(self.repo, REL, 'GREETING = "be skeptical of consensus"\n')
        edits = [{"file": REL, "old_string": "be skeptical of consensus",
                  "new_string": "stress-test the bull case", "rationale": "clarity"}]
        applied, rejected = apply_prompt_edits(edits, self.repo, self.backups)
        self.assertEqual(len(applied), 1)
        self.assertEqual(rejected, [])
        self.assertIn("stress-test the bull case", p.read_text())
        # original backed up
        self.assertTrue((self.backups / REL.replace("/", "__")).exists())

    def test_rejects_non_allowlisted_file(self):
        _make_file(self.repo, "tradingagents/secrets.py", "TOKEN = 1\n")
        edits = [{"file": "tradingagents/secrets.py", "old_string": "TOKEN = 1",
                  "new_string": "TOKEN = 2"}]
        applied, rejected = apply_prompt_edits(edits, self.repo, self.backups)
        self.assertEqual(applied, [])
        self.assertIn("allowlist", rejected[0])

    def test_rejects_missing_anchor(self):
        _make_file(self.repo, REL, "X = 1\n")
        edits = [{"file": REL, "old_string": "not present", "new_string": "Y"}]
        applied, rejected = apply_prompt_edits(edits, self.repo, self.backups)
        self.assertEqual(applied, [])
        self.assertIn("not found", rejected[0])

    def test_rejects_non_unique_anchor(self):
        _make_file(self.repo, REL, "a = 'dup'\nb = 'dup'\n")
        edits = [{"file": REL, "old_string": "dup", "new_string": "uniq"}]
        applied, rejected = apply_prompt_edits(edits, self.repo, self.backups)
        self.assertEqual(applied, [])
        self.assertIn("not unique", rejected[0])

    def test_rejects_placeholder_change(self):
        # Dropping the {ticker} placeholder from an f-string must be refused.
        content = 'PROMPT = f"""Analyze {ticker} now"""\n'
        p = _make_file(self.repo, REL, content)
        edits = [{"file": REL, "old_string": "Analyze {ticker} now",
                  "new_string": "Analyze the stock now"}]
        applied, rejected = apply_prompt_edits(edits, self.repo, self.backups)
        self.assertEqual(applied, [])
        self.assertIn("placeholder", rejected[0])
        self.assertEqual(p.read_text(), content)  # unchanged

    def test_rejects_and_restores_on_syntax_break(self):
        content = 'X = "ok"\n'
        p = _make_file(self.repo, REL, content)
        edits = [{"file": REL, "old_string": '"ok"', "new_string": '"ok" +'}]
        applied, rejected = apply_prompt_edits(edits, self.repo, self.backups)
        self.assertEqual(applied, [])
        self.assertTrue(any("syntax" in r for r in rejected))
        self.assertEqual(p.read_text(), content)  # restored / never broken

    def test_dry_run_changes_nothing(self):
        content = 'GREETING = "hello"\n'
        p = _make_file(self.repo, REL, content)
        edits = [{"file": REL, "old_string": "hello", "new_string": "hi"}]
        applied, rejected = apply_prompt_edits(edits, self.repo, self.backups, dry_run=True)
        self.assertEqual(len(applied), 1)
        self.assertIn("WOULD APPLY", applied[0])
        self.assertEqual(p.read_text(), content)
        self.assertFalse(self.backups.exists())

    def test_preserved_placeholder_edit_applies(self):
        content = 'PROMPT = f"""Analyze {ticker} carefully"""\n'
        p = _make_file(self.repo, REL2, content)
        edits = [{"file": REL2, "old_string": "Analyze {ticker} carefully",
                  "new_string": "Rigorously analyze {ticker} now"}]
        applied, rejected = apply_prompt_edits(edits, self.repo, self.backups)
        self.assertEqual(len(applied), 1, rejected)
        self.assertIn("Rigorously analyze {ticker} now", p.read_text())


@pytest.mark.unit
class ApplyWeightsTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = si.load_weights.__globals__["_WEIGHTS_PATH"]
        # Redirect weight storage to a temp file so the user's real weights are safe.
        si.load_weights.__globals__["_WEIGHTS_PATH"] = Path(self._tmp.name) / "w.json"

    def tearDown(self):
        si.load_weights.__globals__["_WEIGHTS_PATH"] = self._orig
        self._tmp.cleanup()

    def test_applies_and_clamps(self):
        changes = apply_weight_changes({"weights": {"guidance": 1.3, "fundamentals": 99}})
        self.assertTrue(any("guidance" in c for c in changes))
        self.assertTrue(any("fundamentals" in c and "3.0" in c for c in changes))  # clamped to 3.0
        saved = json.loads((si.load_weights.__globals__["_WEIGHTS_PATH"]).read_text())
        self.assertEqual(saved["guidance"], 1.3)
        self.assertEqual(saved["fundamentals"], 3.0)

    def test_nulls_and_missing_skip(self):
        self.assertEqual(apply_weight_changes({"weights": {"beat": None}}), [])
        self.assertEqual(apply_weight_changes({}), [])

    def test_dry_run_does_not_write(self):
        changes = apply_weight_changes({"weights": {"beat": 2.0}}, dry_run=True)
        self.assertTrue(changes)
        self.assertFalse((si.load_weights.__globals__["_WEIGHTS_PATH"]).exists())


@pytest.mark.unit
class ConsolidateTradesTests(unittest.TestCase):
    def test_merges_fills_same_ticker_and_exit(self):
        trades = [
            {"ticker": "NVDA", "exit_date": "2026-05-10", "direction": "BUY",
             "shares": 100, "entry_price": 10.0, "exit_price": 12.0, "pnl": 200.0},
            {"ticker": "NVDA", "exit_date": "2026-05-10", "direction": "BUY",
             "shares": 100, "entry_price": 12.0, "exit_price": 12.0, "pnl": 0.0},
        ]
        groups = consolidate_trades(list(enumerate(trades)), Path("/nonexistent"))
        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertEqual(g["fills"], 2)
        self.assertEqual(g["shares"], 200)
        self.assertAlmostEqual(g["entry_price"], 11.0)  # share-weighted
        self.assertEqual(g["pnl"], 200.0)
        self.assertEqual(g["outcome"], "WIN")
        self.assertFalse(g["reflected"])


class _StubLLM:
    def __init__(self, payload: dict):
        self._payload = payload

    def invoke(self, _messages):
        class R:
            content = "# Report\nLooks fine.\n\n```json\n" + json.dumps(self._payload) + "\n```\n"
        return R()


@pytest.mark.unit
class RunSelfImprovementTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self._orig = si.load_weights.__globals__["_WEIGHTS_PATH"]
        si.load_weights.__globals__["_WEIGHTS_PATH"] = self.repo / "w.json"

    def tearDown(self):
        si.load_weights.__globals__["_WEIGHTS_PATH"] = self._orig
        self._tmp.cleanup()

    def test_end_to_end_writes_report_and_applies(self):
        _make_file(self.repo, REL, 'NOTE = "old guidance line"\n')
        items = [{"ticker": "AAA", "exit_date": "2026-05-01", "direction": "BUY",
                  "pnl": -50, "pnl_pct": -5.0, "outcome": "LOSS",
                  "beat_correct": True, "guide_correct": False,
                  "key_lesson": "guide missed", "content": "post-mortem text"}]
        payload = {
            "weights": {"guidance": 0.5},
            "prompt_edits": [{"file": REL, "old_string": "old guidance line",
                              "new_string": "new guidance line", "rationale": "AAA"}],
            "process_notes": ["consider adding peer read-through"],
        }
        run_dir = self.repo / "run"
        summary = run_self_improvement(
            _StubLLM(payload), items, self.repo, run_dir,
        )
        self.assertTrue((run_dir / "improvement_report.md").exists())
        self.assertTrue((run_dir / "CHANGELOG.md").exists())
        self.assertTrue(any("guidance" in c for c in summary["weight_changes"]))
        self.assertEqual(len(summary["prompt_applied"]), 1)
        self.assertIn("new guidance line", (self.repo / REL).read_text())
        self.assertEqual(summary["process_notes"], ["consider adding peer read-through"])

    def test_dry_run_applies_nothing(self):
        _make_file(self.repo, REL, 'NOTE = "keep me"\n')
        items = [{"ticker": "AAA", "exit_date": "2026-05-01", "direction": "BUY",
                  "pnl": 1, "pnl_pct": 1.0, "outcome": "WIN",
                  "beat_correct": True, "guide_correct": True, "key_lesson": "", "content": "x"}]
        payload = {"weights": {"beat": 2.0},
                   "prompt_edits": [{"file": REL, "old_string": "keep me", "new_string": "changed"}],
                   "process_notes": []}
        summary = run_self_improvement(
            _StubLLM(payload), items, self.repo, self.repo / "run", dry_run=True,
        )
        self.assertEqual(self.repo.joinpath(REL).read_text(), 'NOTE = "keep me"\n')
        self.assertFalse((si.load_weights.__globals__["_WEIGHTS_PATH"]).exists())
        self.assertTrue(summary["dry_run"])

    def test_apply_saved_proposal_verbatim_no_llm(self):
        # A reviewed proposal.json is applied directly — no LLM, exact edits.
        _make_file(self.repo, REL, 'NOTE = "before"\n')
        proposal = {
            "weights": {"setup": 1.4},
            "prompt_edits": [{"file": REL, "old_string": "before", "new_string": "after"}],
            "process_notes": ["note"],
        }
        run_dir = self.repo / "run"
        summary = apply_proposal(proposal, self.repo, run_dir, n_reflections=7)
        self.assertIn("after", (self.repo / REL).read_text())
        self.assertTrue(any("setup" in c for c in summary["weight_changes"]))
        self.assertEqual(len(summary["prompt_applied"]), 1)
        self.assertIsNone(summary["report_path"])  # no report on the apply-only path
        self.assertTrue((run_dir / "CHANGELOG.md").exists())


if __name__ == "__main__":
    unittest.main()
