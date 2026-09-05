"""Unit tests for the dashboard's paged/trimmed payload. No network.

The dashboard page was 27 MB and took seconds to build; these lock in the two
things that fixed it — omitting report bodies, and returning only recent runs —
plus the totals the UI needs to offer "Load more".
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

from cli.commands.reports import _build_reports_data

BRIEF = """# Brief for TICK

Some narrative body that should not be shipped unless asked for.

```json
{"earnings_date": "2026-08-10", "beat_score": 1, "guidance_score": 2,
 "setup_score": 0, "total_score": 3, "signal": "BUY", "confidence": "High",
 "one_liner": "fine"}
```
"""


def make_reports(root: Path, n_runs=3, tickers_per_run=2, decisions=True, trades=0):
    base = root / "earnings"
    for i in range(n_runs):
        run = base / f"earnings_2026-08-{10 + i:02d}_2026081{i}_120000"
        run.mkdir(parents=True)
        (run / "screening_table.md").write_text("| t |\n|---|\n", encoding="utf-8")
        for j in range(tickers_per_run):
            td = run / f"T{i}{j}"
            td.mkdir()
            (td / "earnings_brief.md").write_text(BRIEF, encoding="utf-8")
            if decisions:
                (td / "5_portfolio").mkdir()
                (td / "5_portfolio" / "decision.md").write_text("PM DECISION BODY", encoding="utf-8")
    trades_path = root / "trades.json"
    trades_path.write_text(json.dumps(
        [{"ticker": f"X{i}", "pnl": i, "exit_date": "2026-08-01"} for i in range(trades)]
    ), encoding="utf-8")
    return root, trades_path


def build(root, trades_path, **kw):
    # collect_watchlists walks the same tree; keep it out of these assertions.
    with mock.patch("tradingagents.allocation.watchlist.collect_watchlists", return_value=[]):
        return _build_reports_data(root, trades_path, **kw)


@pytest.mark.unit
class BodyOmissionTests(unittest.TestCase):
    def test_bodies_present_by_default(self):
        """The static build has no server to fetch from, so it must get everything."""
        with tempfile.TemporaryDirectory() as td:
            root, tp = make_reports(Path(td))
            data = build(root, tp)
        tk = data["screening_runs"][0]["tickers"][0]
        self.assertIn("Some narrative body", tk["earnings_brief_md"])
        self.assertEqual(tk["portfolio_decision_md"], "PM DECISION BODY")
        self.assertTrue(data["bodies_included"])

    def test_bodies_omitted_but_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            root, tp = make_reports(Path(td))
            data = build(root, tp, include_bodies=False)
        tk = data["screening_runs"][0]["tickers"][0]
        self.assertIsNone(tk["earnings_brief_md"])
        self.assertIsNone(tk["portfolio_decision_md"])
        self.assertTrue(tk["has_brief"])          # so the UI knows to fetch
        self.assertTrue(tk["has_decision"])
        self.assertFalse(data["bodies_included"])

    def test_has_decision_false_when_there_is_none(self):
        with tempfile.TemporaryDirectory() as td:
            root, tp = make_reports(Path(td), decisions=False)
            data = build(root, tp, include_bodies=False)
        self.assertFalse(data["screening_runs"][0]["tickers"][0]["has_decision"])

    def test_scores_survive_body_omission(self):
        """Omitting bodies must not cost the score metadata parsed out of them."""
        with tempfile.TemporaryDirectory() as td:
            root, tp = make_reports(Path(td))
            data = build(root, tp, include_bodies=False)
        tk = data["screening_runs"][0]["tickers"][0]
        self.assertEqual(tk["total_score"], 3)
        self.assertEqual(tk["signal"], "BUY")
        self.assertEqual(tk["one_liner"], "fine")

    def test_omitting_bodies_shrinks_the_payload(self):
        """Real briefs average ~3 KB and decisions ~3.7 KB — size the fixture to match,
        or the metadata dwarfs the bodies and the ratio means nothing."""
        with tempfile.TemporaryDirectory() as td:
            root, tp = make_reports(Path(td), n_runs=4, tickers_per_run=5)
            for brief in root.rglob("earnings_brief.md"):
                brief.write_text(("narrative paragraph. " * 160) + BRIEF, encoding="utf-8")
            for dec in root.rglob("decision.md"):
                dec.write_text("decision paragraph. " * 190, encoding="utf-8")
            full = len(json.dumps(build(root, tp)))
            trim = len(json.dumps(build(root, tp, include_bodies=False)))
        self.assertLess(trim, full / 4)


@pytest.mark.unit
class RunPagingTests(unittest.TestCase):
    def test_limit_returns_newest_runs_and_the_total(self):
        with tempfile.TemporaryDirectory() as td:
            root, tp = make_reports(Path(td), n_runs=5)
            data = build(root, tp, limit_runs=2)
        self.assertEqual(len(data["screening_runs"]), 2)
        self.assertEqual(data["total_runs"], 5)
        # iter_run_dirs is newest-first
        self.assertEqual(data["screening_runs"][0]["date"], "2026-08-14")

    def test_offset_walks_backwards_without_overlap(self):
        with tempfile.TemporaryDirectory() as td:
            root, tp = make_reports(Path(td), n_runs=5)
            page1 = build(root, tp, limit_runs=2)["screening_runs"]
            page2 = build(root, tp, limit_runs=2, run_offset=2)["screening_runs"]
        ids1 = {r["id"] for r in page1}
        ids2 = {r["id"] for r in page2}
        self.assertEqual(len(ids1 & ids2), 0)
        self.assertEqual(len(ids1 | ids2), 4)

    def test_offset_past_the_end_is_empty_not_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            root, tp = make_reports(Path(td), n_runs=2)
            data = build(root, tp, limit_runs=5, run_offset=99)
        self.assertEqual(data["screening_runs"], [])
        self.assertEqual(data["total_runs"], 2)

    def test_limit_zero_scans_no_runs(self):
        with tempfile.TemporaryDirectory() as td:
            root, tp = make_reports(Path(td), n_runs=3)
            data = build(root, tp, limit_runs=0)
        self.assertEqual(data["screening_runs"], [])
        self.assertEqual(data["total_runs"], 3)


@pytest.mark.unit
class TradeAndReflectionPagingTests(unittest.TestCase):
    def test_trade_limit_keeps_the_most_recent(self):
        """The log is oldest-first, so a limit must take the tail."""
        with tempfile.TemporaryDirectory() as td:
            root, tp = make_reports(Path(td), n_runs=1, trades=10)
            data = build(root, tp, limit_trades=3)
        self.assertEqual([t["ticker"] for t in data["trades"]], ["X7", "X8", "X9"])
        self.assertEqual(data["total_trades"], 10)

    def test_trade_limit_zero_returns_none_of_them(self):
        """trades[-0:] is the whole list — the guard against that lives here."""
        with tempfile.TemporaryDirectory() as td:
            root, tp = make_reports(Path(td), n_runs=1, trades=10)
            data = build(root, tp, limit_trades=0)
        self.assertEqual(data["trades"], [])
        self.assertEqual(data["total_trades"], 10)

    def test_no_limit_returns_every_trade(self):
        with tempfile.TemporaryDirectory() as td:
            root, tp = make_reports(Path(td), n_runs=1, trades=10)
            data = build(root, tp)
        self.assertEqual(len(data["trades"]), 10)

    def test_reflection_total_reported_even_when_limited(self):
        with tempfile.TemporaryDirectory() as td:
            root, tp = make_reports(Path(td), n_runs=1)
            refl = root / "reflections"
            for i in range(4):
                d = refl / f"TICK_2026-08-0{i + 1}"
                d.mkdir(parents=True)
                (d / "post_mortem.md").write_text("post mortem", encoding="utf-8")
            data = build(root, tp, limit_reflections=2)
        self.assertEqual(len(data["reflections"]), 2)
        self.assertEqual(data["total_reflections"], 4)


@pytest.mark.unit
class WholeHistoryStatsTests(unittest.TestCase):
    """Lifetime figures must not depend on how much of the log is displayed.

    Regression: `stats` was computed from the truncated `trades` list, so the
    Overview reported $28k of lifetime P&L instead of $143k the moment the
    dashboard started sending only recent fills.
    """

    def _with_pnl(self, root: Path, pnls):
        tp = root / "trades.json"
        tp.write_text(json.dumps(
            [{"ticker": f"T{i}", "pnl": v, "exit_date": f"2026-01-{i % 28 + 1:02d}",
              "shares": 1, "entry_price": 10} for i, v in enumerate(pnls)]
        ), encoding="utf-8")
        return tp

    def test_total_pnl_ignores_the_display_limit(self):
        with tempfile.TemporaryDirectory() as td:
            root, _ = make_reports(Path(td), n_runs=1)
            tp = self._with_pnl(root, [100] * 50)
            full = build(root, tp)["stats"]
            trimmed = build(root, tp, limit_trades=5)["stats"]
        self.assertEqual(full["total_pnl"], 5000)
        self.assertEqual(trimmed["total_pnl"], 5000)          # not 500
        self.assertEqual(trimmed["wins"], full["wins"])
        self.assertEqual(trimmed["win_rate"], full["win_rate"])

    def test_win_rate_ignores_the_display_limit(self):
        """Recent fills are unrepresentative by construction here."""
        with tempfile.TemporaryDirectory() as td:
            root, _ = make_reports(Path(td), n_runs=1)
            tp = self._with_pnl(root, [-10] * 40 + [10] * 10)   # tail is all wins
            trimmed = build(root, tp, limit_trades=10)["stats"]
        self.assertEqual(trimmed["wins"], 10)
        self.assertEqual(trimmed["losses"], 40)
        self.assertAlmostEqual(trimmed["win_rate"], 20.0)       # not 100%

    def test_trades_array_still_honours_the_limit(self):
        with tempfile.TemporaryDirectory() as td:
            root, _ = make_reports(Path(td), n_runs=1)
            tp = self._with_pnl(root, [1] * 50)
            data = build(root, tp, limit_trades=5)
        self.assertEqual(len(data["trades"]), 5)
        self.assertEqual(data["total_trades"], 50)


@pytest.mark.unit
class WatchlistRefreshTests(unittest.TestCase):
    def test_refresh_flag_is_passed_through(self):
        """The live-quote round-trip is most of the build time; the server skips it."""
        with tempfile.TemporaryDirectory() as td:
            root, tp = make_reports(Path(td), n_runs=1)
            with mock.patch("tradingagents.allocation.watchlist.collect_watchlists",
                            return_value=[]) as cw:
                _build_reports_data(root, tp, refresh_watchlist=False)
        self.assertEqual(cw.call_args.kwargs.get("refresh"), False)

    def test_refresh_on_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            root, tp = make_reports(Path(td), n_runs=1)
            with mock.patch("tradingagents.allocation.watchlist.collect_watchlists",
                            return_value=[]) as cw:
                _build_reports_data(root, tp)
        self.assertEqual(cw.call_args.kwargs.get("refresh"), True)


if __name__ == "__main__":
    unittest.main()
