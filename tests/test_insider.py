"""Unit tests for the insider-signal engine (#18) — pure logic, no network."""

import unittest

import pytest

from tradingagents.allocation.insider import (
    _classify,
    compute_insider_signal,
    format_insider,
)


def _txn(date, insider, kind, value):
    return {"date": date, "insider": insider, "kind": kind, "value": value}


@pytest.mark.unit
class ClassifyTests(unittest.TestCase):
    def test_buy_sell_other(self):
        self.assertEqual(_classify("Purchase at price 10.00 per share."), "buy")
        self.assertEqual(_classify("Sale at price 10.00 per share."), "sell")
        self.assertEqual(_classify("Stock Gift at price 0.00 per share."), "other")
        self.assertEqual(_classify("Exercise of derivative security."), "other")
        self.assertEqual(_classify(None), "other")


@pytest.mark.unit
class ClusterAndReversalTests(unittest.TestCase):
    ASOF = "2026-06-20"

    def test_cluster_buy_three_distinct_buyers(self):
        txns = [
            _txn("2026-06-10", "Alice", "buy", 500_000),
            _txn("2026-06-05", "Bob", "buy", 300_000),
            _txn("2026-05-30", "Carol", "buy", 200_000),
        ]
        sig = compute_insider_signal(txns, asof=self.ASOF)
        self.assertTrue(sig["cluster_buy"])
        self.assertEqual(sig["cluster_buyers"], 3)
        self.assertEqual(sig["signal"], "cluster buy")
        self.assertTrue(any("cluster buy" in f for f in sig["flags"]))

    def test_two_buyers_not_a_cluster(self):
        txns = [
            _txn("2026-06-10", "Alice", "buy", 500_000),
            _txn("2026-06-05", "Bob", "buy", 300_000),
        ]
        sig = compute_insider_signal(txns, asof=self.ASOF)
        self.assertFalse(sig["cluster_buy"])
        self.assertEqual(sig["signal"], "net buying")

    def test_reversal_sold_then_bought(self):
        txns = [
            _txn("2026-06-10", "Alice", "buy", 400_000),   # recent buy
            _txn("2026-01-15", "Alice", "sell", 900_000),  # older sell
        ]
        sig = compute_insider_signal(txns, asof=self.ASOF)
        self.assertTrue(sig["reversal"])
        self.assertIn("Alice", sig["reversal_insiders"])
        self.assertEqual(sig["signal"], "reversal")

    def test_no_reversal_when_only_recent_activity(self):
        txns = [_txn("2026-06-10", "Alice", "buy", 400_000)]
        sig = compute_insider_signal(txns, asof=self.ASOF)
        self.assertFalse(sig["reversal"])

    def test_cluster_takes_priority_over_reversal(self):
        txns = [
            _txn("2026-06-10", "Alice", "buy", 400_000),
            _txn("2026-06-08", "Bob", "buy", 400_000),
            _txn("2026-06-06", "Carol", "buy", 400_000),
            _txn("2026-01-10", "Alice", "sell", 500_000),  # also a reversal
        ]
        sig = compute_insider_signal(txns, asof=self.ASOF)
        self.assertTrue(sig["cluster_buy"])
        self.assertTrue(sig["reversal"])
        self.assertEqual(sig["signal"], "cluster buy")


@pytest.mark.unit
class NetAndNoiseTests(unittest.TestCase):
    ASOF = "2026-06-20"

    def test_notable_buy_flagged(self):
        sig = compute_insider_signal([_txn("2026-06-10", "Alice", "buy", 3_000_000)], asof=self.ASOF)
        self.assertTrue(any("buy by" in f for f in sig["flags"]))
        self.assertEqual(len(sig["notable_buys"]), 1)

    def test_heavy_selling_no_buys_is_caution(self):
        sig = compute_insider_signal([_txn("2026-06-10", "Alice", "sell", 8_000_000)], asof=self.ASOF)
        self.assertEqual(sig["signal"], "net selling")
        self.assertTrue(any("heavy net selling" in f for f in sig["flags"]))

    def test_routine_small_selling_is_quiet(self):
        sig = compute_insider_signal([_txn("2026-06-10", "Alice", "sell", 50_000)], asof=self.ASOF)
        self.assertEqual(sig["signal"], "quiet")
        self.assertEqual(sig["flags"], [])

    def test_gifts_and_exercises_ignored(self):
        txns = [_txn("2026-06-10", "Alice", "other", 999_000_000)]
        sig = compute_insider_signal(txns, asof=self.ASOF)
        self.assertEqual(sig["n_buys"], 0)
        self.assertEqual(sig["n_sells"], 0)
        self.assertEqual(sig["signal"], "quiet")

    def test_empty_input(self):
        sig = compute_insider_signal([])
        self.assertEqual(sig["signal"], "quiet")
        self.assertIsNone(sig["asof"])

    def test_old_transactions_outside_recent_window_dont_count_as_recent(self):
        # A buy 200d ago is older than the 90d recent window.
        sig = compute_insider_signal([_txn("2025-12-01", "Alice", "buy", 5_000_000)], asof=self.ASOF)
        self.assertEqual(sig["n_buys"], 0)  # not in recent window
        self.assertEqual(sig["signal"], "quiet")


@pytest.mark.unit
class FormatTests(unittest.TestCase):
    def test_format_with_flags(self):
        sig = compute_insider_signal([
            _txn("2026-06-10", "Alice", "buy", 2_000_000),
            _txn("2026-06-08", "Bob", "buy", 1_500_000),
            _txn("2026-06-06", "Carol", "buy", 1_200_000),
        ], asof="2026-06-20")
        line = format_insider(sig)
        self.assertIn("cluster buy", line)
        self.assertIn("buyers", line)

    def test_format_unavailable(self):
        self.assertEqual(format_insider(None), "Not available")
        self.assertEqual(format_insider({"asof": None}), "Not available")


if __name__ == "__main__":
    unittest.main()
