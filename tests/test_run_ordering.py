"""Run ordering + the calendar's screening index. No network.

Sorting run dirs by raw name looked equivalent to sorting by date but was not:
the prefix sorts first, so every `screening_*` run outranked every `earnings_*`
one. With the dashboard loading only the newest page, five months of August runs
sat below May ones and the earnings calendar matched none of them.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

from tradingagents.reports_layout import iter_run_dirs, run_sort_key

BRIEF = """```json
{"earnings_date": "2026-08-13", "beat_score": 1, "guidance_score": 0,
 "setup_score": 2, "total_score": 3, "signal": "BUY", "confidence": "High",
 "one_liner": "ok"}
```"""


def mkrun(base: Path, name: str, tickers=()):
    d = base / name
    d.mkdir(parents=True)
    for t in tickers:
        (d / t).mkdir()
        (d / t / "earnings_brief.md").write_text(BRIEF, encoding="utf-8")
    return d


@pytest.mark.unit
class RunSortKeyTests(unittest.TestCase):
    def test_prefix_does_not_outrank_date(self):
        """The actual regression: 's' > 'e' put every screening_ run first."""
        names = [
            "screening_2026-05-19_20260517_205909",
            "earnings_2026-08-13_20260812_205756",
            "screening_2026-05-08_20260508_192038",
            "earnings_2026-08-12_20260812_110714",
        ]
        ordered = sorted(names, key=run_sort_key, reverse=True)
        self.assertEqual(ordered[0], "earnings_2026-08-13_20260812_205756")
        self.assertEqual(ordered[1], "earnings_2026-08-12_20260812_110714")
        self.assertTrue(all(n.startswith("screening_") for n in ordered[2:]))

    def test_same_day_runs_break_ties_on_timestamp(self):
        a = "earnings_2026-08-11_20260811_132819"
        b = "earnings_2026-08-11_20260811_141343"
        self.assertEqual(sorted([a, b], key=run_sort_key, reverse=True)[0], b)

    def test_unparseable_names_sort_last_without_raising(self):
        names = ["earnings_2026-08-13_20260812_205756", "weird", "earnings_"]
        ordered = sorted(names, key=run_sort_key, reverse=True)
        self.assertEqual(ordered[0], "earnings_2026-08-13_20260812_205756")

    def test_iter_run_dirs_returns_newest_first(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "earnings"
            mkrun(base, "screening_2026-05-19_20260517_205909")
            mkrun(base, "earnings_2026-08-13_20260812_205756")
            mkrun(base, "earnings_2026-07-01_20260701_120000")
            got = [d.name for d in iter_run_dirs(td)]
        self.assertEqual(got[0], "earnings_2026-08-13_20260812_205756")
        self.assertEqual(got[-1], "screening_2026-05-19_20260517_205909")


@pytest.mark.unit
class ScreeningIndexTests(unittest.TestCase):
    """The calendar's index must not depend on which runs are paged into the UI."""

    def _build(self, root, **kw):
        from cli.commands.reports import build_screening_index
        return build_screening_index(root, **kw)

    def test_indexes_every_run_not_just_recent_ones(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "earnings"
            mkrun(base, "earnings_2026-08-13_20260812_205756", ["AAA"])
            mkrun(base, "earnings_2026-06-01_20260601_120000", ["BBB"])
            idx = self._build(Path(td))
        self.assertIn("AAA", idx)
        self.assertIn("BBB", idx)

    def test_since_filters_old_runs(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "earnings"
            mkrun(base, "earnings_2026-08-13_20260812_205756", ["AAA"])
            mkrun(base, "earnings_2026-06-01_20260601_120000", ["BBB"])
            idx = self._build(Path(td), since="2026-07-01")
        self.assertIn("AAA", idx)
        self.assertNotIn("BBB", idx)

    def test_ticker_filter_scopes_to_the_calendar(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "earnings"
            mkrun(base, "earnings_2026-08-13_20260812_205756", ["AAA", "BBB"])
            idx = self._build(Path(td), tickers={"AAA"})
        self.assertEqual(set(idx), {"AAA"})

    def test_entries_carry_what_a_calendar_row_shows(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "earnings"
            mkrun(base, "earnings_2026-08-13_20260812_205756", ["AAA"])
            row = self._build(Path(td))["AAA"][0]
        for key in ("run_id", "date", "signal", "confidence", "total_score", "one_liner"):
            self.assertIn(key, row)
        self.assertEqual(row["signal"], "BUY")
        self.assertEqual(row["total_score"], 3)

    def test_multiple_screenings_come_back_newest_first(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "earnings"
            mkrun(base, "earnings_2026-08-13_20260812_205756", ["AAA"])
            mkrun(base, "earnings_2026-07-20_20260720_120000", ["AAA"])
            rows = self._build(Path(td))["AAA"]
        self.assertEqual([r["date"] for r in rows], ["2026-08-13", "2026-07-20"])

    def test_no_bodies_in_the_index(self):
        """It exists to stay small — report text belongs behind /api/report."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "earnings"
            mkrun(base, "earnings_2026-08-13_20260812_205756", ["AAA"])
            row = self._build(Path(td))["AAA"][0]
        self.assertNotIn("earnings_brief_md", row)
        self.assertNotIn("portfolio_decision_md", row)


if __name__ == "__main__":
    unittest.main()
