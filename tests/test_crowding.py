"""Unit tests for the crowding / run-up gate (#14c) — pure logic, no network."""

import unittest

import pytest

from tradingagents.allocation.crowding import compute_crowding_flags, format_crowding


@pytest.mark.unit
class CrowdingFlagsTests(unittest.TestCase):
    def test_no_flags_when_quiet(self):
        c = {"runup_1m_pct": 3.0, "runup_1m_vs_sector": 1.0,
             "dist_52w_high_pct": 20.0, "revision_up_30d": 0, "revision_down_30d": 1}
        self.assertEqual(compute_crowding_flags(c), [])

    def test_absolute_runup_flag(self):
        c = {"runup_1m_pct": 15.0}
        flags = compute_crowding_flags(c)
        self.assertTrue(any("run-up" in f for f in flags))

    def test_vs_sector_runup_flag(self):
        c = {"runup_1m_pct": 9.0, "runup_1m_vs_sector": 10.0}
        flags = compute_crowding_flags(c)
        self.assertTrue(any("vs sector" in f for f in flags))

    def test_near_52w_high_flag(self):
        c = {"dist_52w_high_pct": 2.0}
        flags = compute_crowding_flags(c)
        self.assertTrue(any("52w high" in f for f in flags))

    def test_far_from_high_no_flag(self):
        self.assertEqual(compute_crowding_flags({"dist_52w_high_pct": 10.0}), [])

    def test_revision_momentum_flag(self):
        c = {"revision_up_30d": 4, "revision_down_30d": 1}
        flags = compute_crowding_flags(c)
        self.assertTrue(any("up-revisions" in f for f in flags))

    def test_revisions_not_flagged_when_downs_dominate(self):
        c = {"revision_up_30d": 3, "revision_down_30d": 5}
        self.assertEqual(compute_crowding_flags(c), [])

    def test_multiple_flags_accumulate(self):
        c = {"runup_1m_pct": 20.0, "runup_1m_vs_sector": 12.0,
             "dist_52w_high_pct": 1.0, "revision_up_30d": 5, "revision_down_30d": 0}
        self.assertEqual(len(compute_crowding_flags(c)), 4)

    def test_none_fields_safe(self):
        c = {"runup_1m_pct": None, "runup_1m_vs_sector": None,
             "dist_52w_high_pct": None, "revision_up_30d": None}
        self.assertEqual(compute_crowding_flags(c), [])


@pytest.mark.unit
class FormatCrowdingTests(unittest.TestCase):
    def test_full_line(self):
        c = {"sector_etf": "XLK", "runup_1m_pct": 14.0, "runup_1m_vs_sector": 9.0,
             "runup_3m_pct": 25.0, "dist_52w_high_pct": 2.0,
             "flags": ["1m run-up +14%", "within 2% of 52w high"]}
        line = format_crowding(c)
        self.assertIn("XLK", line)
        self.assertIn("FLAGS:", line)
        self.assertIn("52w high", line)

    def test_missing_fields_degrade(self):
        line = format_crowding({"sector_etf": "SPY"})
        self.assertIn("n/a", line)
        self.assertNotIn("FLAGS:", line)  # no flags key

    def test_none(self):
        self.assertEqual(format_crowding(None), "Not available")


if __name__ == "__main__":
    unittest.main()
