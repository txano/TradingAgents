"""Unit tests for the trade-log schema v2 + enrichment (#17). No network."""

import json
import unittest
from datetime import date

import pandas as pd
import pytest

from tradingagents import trade_log as tl


@pytest.mark.unit
class EnsureV2Tests(unittest.TestCase):
    def test_adds_all_fields_and_is_idempotent(self):
        t = {"ticker": "MU", "pnl": 123.0}
        tl.ensure_v2(t)
        for key in tl.V2_FIELDS:
            self.assertIn(key, t)
        self.assertEqual(t["schema_version"], 2)
        self.assertEqual(t["pnl_final"], 123.0)  # mirrors pnl when unset

        # Second call must not clobber a value we set in between.
        t["beat_eps"] = True
        tl.ensure_v2(t)
        self.assertIs(t["beat_eps"], True)

    def test_pnl_final_not_overwritten(self):
        t = {"pnl": 5.0, "pnl_final": 9.0}
        tl.ensure_v2(t)
        self.assertEqual(t["pnl_final"], 9.0)


@pytest.mark.unit
class FindScreeningRunTests(unittest.TestCase):
    def _make_run(self, root, name, tickers):
        run = root / name
        for tk in tickers:
            (run / tk).mkdir(parents=True)
        return run

    def test_matches_both_screening_and_earnings_layouts(self, tmp_path=None):
        import tempfile
        from pathlib import Path
        root = Path(tempfile.mkdtemp())
        # Legacy screening_* and current earnings/earnings_* both hold the ticker.
        self._make_run(root, "screening_2026-05-01_20260501_101010", ["MU"])
        (root / "earnings").mkdir()
        self._make_run(root / "earnings", "earnings_2026-06-10_20260608_181111", ["MU"])

        # ref after both → newest (June) wins
        d = tl.find_screening_run("MU", "2026-06-20", root)
        self.assertEqual(d.parent.name, "earnings_2026-06-10_20260608_181111")

        # ref before June run → only May qualifies
        d2 = tl.find_screening_run("MU", "2026-05-15", root)
        self.assertEqual(d2.parent.name, "screening_2026-05-01_20260501_101010")

        # unknown ticker → None
        self.assertIsNone(tl.find_screening_run("ZZZZ", "2026-06-20", root))


@pytest.mark.unit
class EnrichFromArtifactsTests(unittest.TestCase):
    def test_pulls_context_from_saved_json(self):
        import tempfile
        from pathlib import Path
        root = Path(tempfile.mkdtemp())
        tdir = root / "earnings" / "earnings_2026-06-10_20260608_1" / "MU"
        tdir.mkdir(parents=True)
        (tdir / "pricing.json").write_text(json.dumps({"implied_move_pct": 8.5}))
        (tdir / "crowding.json").write_text(json.dumps({
            "runup_1m_pct": 14.0, "runup_1m_vs_sector": 9.0,
            "dist_52w_high_pct": 2.0, "revision_up_30d": 5, "revision_down_30d": 1,
        }))
        (tdir / "asymmetry.json").write_text(json.dumps({"coverage_ratio": 1.3}))

        t = {"ticker": "MU", "exit_date": "2026-06-20"}
        tl.enrich_from_artifacts(t, root)

        self.assertEqual(t["screening_run"], "earnings_2026-06-10_20260608_1")
        self.assertEqual(t["implied_move_pct"], 8.5)
        self.assertEqual(t["runup_1m_pct"], 14.0)
        self.assertEqual(t["runup_vs_sector_1m"], 9.0)
        self.assertEqual(t["dist_52w_high_pct"], 2.0)
        self.assertEqual(t["revision_direction_30d"], "up")
        self.assertEqual(t["coverage_ratio"], 1.3)

    def test_no_run_leaves_fields_none(self):
        import tempfile
        from pathlib import Path
        root = Path(tempfile.mkdtemp())
        t = {"ticker": "MU", "exit_date": "2026-06-20"}
        tl.enrich_from_artifacts(t, root)
        self.assertIsNone(t["implied_move_pct"])
        self.assertEqual(t["schema_version"], 2)  # still normalised


@pytest.mark.unit
class MovesAfterTests(unittest.TestCase):
    def _hist(self, rows):
        idx = pd.to_datetime([d for d, _ in rows])
        return pd.DataFrame({"Close": [c for _, c in rows]}, index=idx)

    def test_d1_uses_second_bar_after_target(self):
        # close-before = 100 on day before; print day, then +1/+5 bars
        rows = [("2026-06-08", 100.0), ("2026-06-09", 100.0),  # target day
                ("2026-06-10", 90.0)]                            # d1 = -10%
        out = tl._moves_after(self._hist(rows), date(2026, 6, 9))
        self.assertEqual(out["move_d1"], -10.0)
        self.assertIsNone(out["move_d20"])  # not enough bars


if __name__ == "__main__":
    unittest.main()
