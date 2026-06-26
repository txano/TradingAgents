"""Unit tests for the peer earnings read-through (#9). Pure logic, no network."""

import unittest
from datetime import date

import pytest

from tradingagents.earnings.peers import (
    assemble_readthrough,
    beat_and_fell,
    format_peer_oneliner,
    format_peer_readthrough,
    get_peers,
)


def _r(peer, d, beat, surprise, move):
    return {"peer": peer, "date": d, "beat": beat,
            "surprise_pct": surprise, "day1_move_pct": move}


@pytest.mark.unit
class BeatAndFellTests(unittest.TestCase):
    def test_flag(self):
        self.assertTrue(beat_and_fell(_r("X", "2026-06-01", True, 2.0, -3.0)))
        self.assertFalse(beat_and_fell(_r("X", "2026-06-01", True, 2.0, 3.0)))   # beat & rose
        self.assertFalse(beat_and_fell(_r("X", "2026-06-01", False, -2.0, -3.0)))  # miss
        self.assertFalse(beat_and_fell(_r("X", "2026-06-01", True, 2.0, None)))   # no move


@pytest.mark.unit
class AssembleTests(unittest.TestCase):
    def test_lookback_filter_and_flags(self):
        recs = [
            _r("NVDA", "2026-06-10", True, 5.0, 3.0),    # 8d ago
            _r("INTC", "2026-06-05", True, 1.0, -4.0),   # 13d ago — beat-and-fell
            _r("MU",   "2026-05-01", False, -2.0, -8.0),  # 48d ago — dropped
            _r("QCOM", "2026-06-12", False, -1.0, -6.0),  # 6d ago — miss
        ]
        d = assemble_readthrough("AMD", recs, date(2026, 6, 18), lookback_days=35)
        self.assertEqual(d["n_reported"], 3)
        self.assertTrue(d["has_map"])
        self.assertTrue(d["sector_bar_elevated"])
        self.assertTrue(d["any_miss"])
        # sorted most-recent first
        self.assertEqual([p["peer"] for p in d["peers"]], ["QCOM", "NVDA", "INTC"])

    def test_no_recent_prints(self):
        recs = [_r("NVDA", "2026-01-01", True, 5.0, 3.0)]
        d = assemble_readthrough("AMD", recs, date(2026, 6, 18), lookback_days=35)
        self.assertEqual(d["n_reported"], 0)
        self.assertFalse(d["sector_bar_elevated"])

    def test_unmapped_ticker_has_no_map(self):
        d = assemble_readthrough("ZZZZ", [], date(2026, 6, 18))
        self.assertFalse(d["has_map"])


@pytest.mark.unit
class FormatTests(unittest.TestCase):
    def test_block_and_oneliner(self):
        recs = [_r("INTC", "2026-06-05", True, 1.0, -4.0)]
        d = assemble_readthrough("AMD", recs, date(2026, 6, 18))
        block = format_peer_readthrough(d)
        self.assertIn("BEAT-AND-FELL", block)
        self.assertIn("elevated", block.lower())
        one = format_peer_oneliner(d)
        self.assertIn("INTC", one)
        self.assertIn("elevated bar", one)

    def test_unmapped_messages(self):
        d = assemble_readthrough("ZZZZ", [], date(2026, 6, 18))
        self.assertEqual(format_peer_oneliner(d), "No peer map")
        self.assertIn("No curated peer map", format_peer_readthrough(d))


@pytest.mark.unit
class PeerMapTests(unittest.TestCase):
    def test_case_insensitive_lookup(self):
        self.assertEqual(get_peers("amd"), get_peers("AMD"))
        self.assertTrue(get_peers("AMD"))
        self.assertEqual(get_peers("NOPE"), [])


if __name__ == "__main__":
    unittest.main()
