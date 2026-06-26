"""Unit tests for the canonical reports layout helper."""

import tempfile
import unittest
from pathlib import Path

import pytest

from tradingagents.reports_layout import RUN_PREFIXES, iter_run_dirs, runs_root


@pytest.mark.unit
class ReportsLayoutTests(unittest.TestCase):
    def test_runs_root(self):
        self.assertEqual(runs_root("reports"), Path("reports") / "earnings")

    def test_iter_finds_both_prefixes_and_legacy_root(self):
        root = Path(tempfile.mkdtemp())
        er = root / "earnings"
        (er / "screening_2026-05-01_aaa").mkdir(parents=True)
        (er / "earnings_2026-06-10_bbb").mkdir(parents=True)
        (er / "not_a_run").mkdir(parents=True)          # ignored — wrong prefix
        (root / "screening_2026-04-01_legacy").mkdir()   # legacy repo-root run

        runs = iter_run_dirs(root)
        names = [d.name for d in runs]
        self.assertIn("screening_2026-05-01_aaa", names)
        self.assertIn("earnings_2026-06-10_bbb", names)
        self.assertIn("screening_2026-04-01_legacy", names)
        self.assertNotIn("not_a_run", names)
        # newest-first by name
        self.assertEqual(names, sorted(names, reverse=True))

    def test_empty_reports_dir(self):
        root = Path(tempfile.mkdtemp())
        self.assertEqual(iter_run_dirs(root), [])

    def test_run_prefixes(self):
        self.assertEqual(RUN_PREFIXES, ("screening_", "earnings_"))


if __name__ == "__main__":
    unittest.main()
