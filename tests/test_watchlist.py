"""Unit tests for the #19 wait-and-decide watchlist. No network."""

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import pytest

from tradingagents.allocation import watchlist
from tradingagents.allocation.watchlist import (
    WATCH_DEFAULT_EXPIRY_SESSIONS,
    build_watchlist,
    collect_watchlists,
    entry_key,
    entry_status,
    load_states,
    load_watchlist,
    save_watchlist,
    set_entry_state,
)


@pytest.fixture(autouse=True)
def _no_cache_leak():
    """Quotes and bars are cached process-wide; one test's bars must not answer
    another's fetch (a mocked-empty history once read back a previous test's)."""
    watchlist.clear_caches()
    yield
    watchlist.clear_caches()


def _entry(**over):
    base = {
        "ticker": "AAA",
        "trigger_price": 90.0,
        "watch_amount": 10_000,
        "expiry_sessions": 5,
        "earnings_date": "2026-07-10",  # a Friday
        "spot_at_screen": 100.0,
    }
    base.update(over)
    return base


def _bar(d, low, close=None):
    return {"date": d, "low": low, "close": close if close is not None else low + 1}


@pytest.mark.unit
class BuildWatchlistTests(unittest.TestCase):
    CONTEXTS = [
        {"ticker": "AAA", "earnings_date": "2026-07-22", "spot_price": 100.0,
         "implied_move_pct": 6.0, "one_liner": "quality name"},
    ]

    def test_extracts_watch_rows_only(self):
        alloc = {"allocations": [
            {"ticker": "AAA", "direction": "WATCH", "amount": 0,
             "trigger_price": 92.0, "watch_amount": 8_000,
             "watch_expiry_sessions": 3, "rationale": "buy the fade"},
            {"ticker": "BBB", "direction": "BUY", "amount": 10_000},
        ]}
        entries = build_watchlist(alloc, self.CONTEXTS, trade_date="2026-07-18")
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["ticker"], "AAA")
        self.assertEqual(e["trigger_price"], 92.0)
        self.assertEqual(e["watch_amount"], 8_000)
        self.assertEqual(e["expiry_sessions"], 3)
        self.assertEqual(e["thesis"], "buy the fade")
        self.assertEqual(e["earnings_date"], "2026-07-22")
        self.assertEqual(e["spot_at_screen"], 100.0)
        self.assertEqual(e["created"], "2026-07-18")

    def test_invalid_expiry_falls_back_to_default(self):
        alloc = {"allocations": [
            {"ticker": "AAA", "direction": "WATCH", "trigger_price": 92.0,
             "watch_amount": 8_000, "watch_expiry_sessions": 99},
        ]}
        entries = build_watchlist(alloc, self.CONTEXTS)
        self.assertEqual(entries[0]["expiry_sessions"], WATCH_DEFAULT_EXPIRY_SESSIONS)

    def test_empty_alloc_yields_empty(self):
        self.assertEqual(build_watchlist({}, self.CONTEXTS), [])
        self.assertEqual(build_watchlist({"allocations": []}, self.CONTEXTS), [])


@pytest.mark.unit
class EntryStatusTests(unittest.TestCase):
    def test_pending_before_earnings(self):
        s = entry_status(_entry(), today=date(2026, 7, 9))
        self.assertEqual(s["status"], "PENDING")
        s = entry_status(_entry(), today=date(2026, 7, 10))  # earnings day itself
        self.assertEqual(s["status"], "PENDING")

    def test_armed_inside_window_above_trigger(self):
        bars = [_bar("2026-07-13", 95.0, close=96.0), _bar("2026-07-14", 94.0, close=95.5)]
        s = entry_status(_entry(), today=date(2026, 7, 14), bars=bars)
        self.assertEqual(s["status"], "ARMED")
        self.assertEqual(s["sessions_elapsed"], 2)
        self.assertEqual(s["sessions_left"], 3)
        self.assertEqual(s["current_price"], 95.5)
        # 95.5 / 90 − 1 ≈ +6.1% above the trigger
        self.assertAlmostEqual(s["distance_pct"], 6.1, places=1)

    def test_triggered_when_low_touches_trigger(self):
        bars = [_bar("2026-07-13", 95.0), _bar("2026-07-14", 89.5, close=93.0)]
        s = entry_status(_entry(), today=date(2026, 7, 14), bars=bars)
        self.assertEqual(s["status"], "TRIGGERED")
        self.assertEqual(s["hit_date"], "2026-07-14")

    def test_expired_after_window_without_hit(self):
        bars = [_bar(f"2026-07-{d}", 95.0) for d in (13, 14, 15, 16, 17)]
        s = entry_status(_entry(), today=date(2026, 7, 20), bars=bars)
        self.assertEqual(s["status"], "EXPIRED")
        self.assertEqual(s["sessions_left"], 0)

    def test_hit_beyond_window_does_not_trigger(self):
        # 6th bar dips below trigger but the window is 5 sessions
        bars = [_bar(f"2026-07-{d}", 95.0) for d in (13, 14, 15, 16, 17)] + [_bar("2026-07-20", 85.0)]
        s = entry_status(_entry(), today=date(2026, 7, 21), bars=bars)
        self.assertEqual(s["status"], "EXPIRED")

    def test_no_bars_falls_back_to_business_days(self):
        # Fri 2026-07-10 → Tue 2026-07-14 = 2 business days elapsed, no price data
        s = entry_status(_entry(), today=date(2026, 7, 14), bars=None)
        self.assertEqual(s["status"], "ARMED")
        self.assertEqual(s["sessions_elapsed"], 2)
        self.assertIsNone(s["current_price"])

    def test_missing_trigger_or_date_is_unknown(self):
        self.assertEqual(entry_status(_entry(trigger_price=None))["status"], "UNKNOWN")
        self.assertEqual(entry_status(_entry(earnings_date=None))["status"], "UNKNOWN")


@pytest.mark.unit
class PersistenceTests(unittest.TestCase):
    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            entries = [_entry()]
            save_watchlist(entries, td)
            self.assertEqual(load_watchlist(td), entries)

    def test_no_entries_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            save_watchlist([], td)
            self.assertFalse((Path(td) / "watchlist.json").exists())
            self.assertEqual(load_watchlist(td), [])

    def test_corrupt_file_yields_empty(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "watchlist.json").write_text("{not json", encoding="utf-8")
            self.assertEqual(load_watchlist(td), [])


@pytest.mark.unit
class UserStateTests(unittest.TestCase):
    """The purchased/dismissed overlay the dashboard buttons write."""

    def test_set_load_and_clear(self):
        with tempfile.TemporaryDirectory() as td:
            set_entry_state("run_1", "aaa", "purchased", reports_root=td, price=91.5)
            states = load_states(td)
            key = entry_key("run_1", "AAA")
            self.assertEqual(states[key]["state"], "purchased")
            self.assertEqual(states[key]["price"], 91.5)
            self.assertTrue(states[key]["at"])

            set_entry_state("run_1", "AAA", None, reports_root=td)
            self.assertEqual(load_states(td), {})

    def test_state_file_lives_in_reports_root(self):
        with tempfile.TemporaryDirectory() as td:
            set_entry_state("run_1", "AAA", "dismissed", reports_root=td)
            self.assertTrue((Path(td) / "watchlist_state.json").exists())

    def test_unknown_state_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                set_entry_state("run_1", "AAA", "bought", reports_root=td)

    def test_missing_state_file_yields_empty(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(load_states(td), {})

    def test_entries_carry_state_and_dismissed_skips_fetch(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "earnings" / "screening_2026-07-20_x"
            run.mkdir(parents=True)
            # Relative to today: collect_watchlists() skips the price fetch on
            # rows older than max_age_days, so a hardcoded date would make this
            # test pass only for its first 45 days.
            recent = (date.today() - timedelta(days=3)).isoformat()
            save_watchlist([_entry(ticker="AAA", earnings_date=recent),
                            _entry(ticker="BBB", earnings_date=recent)], run)
            set_entry_state(run.name, "BBB", "dismissed", reports_root=td)

            calls = []
            with mock.patch.object(watchlist, "refresh_entry",
                                   side_effect=lambda e, **kw: (calls.append(e["ticker"]), e)[1]):
                rows = {r["ticker"]: r for r in collect_watchlists(td)}

            self.assertEqual(calls, ["AAA"])           # dismissed row costs no network
            self.assertIsNone(rows["AAA"]["user_state"])
            self.assertEqual(rows["BBB"]["user_state"], "dismissed")


@pytest.mark.unit
class LivePriceTests(unittest.TestCase):
    """`Now` prefers the live quote; triggers still key off the daily bars."""

    def setUp(self):
        watchlist._QUOTE_CACHE.clear()

    def test_live_price_overrides_close(self):
        entry = _entry()
        with mock.patch.object(watchlist, "fetch_live_price", return_value=95.0), \
             mock.patch("yfinance.Ticker") as tk:
            tk.return_value.history.return_value = None
            out = watchlist.refresh_entry(entry, today=date(2026, 7, 13))
        self.assertEqual(out["current_price"], 95.0)
        self.assertEqual(out["live_price"], 95.0)
        self.assertEqual(out["price_source"], "live")
        self.assertAlmostEqual(out["distance_pct"], 5.6, places=1)   # 95 vs 90 trigger
        self.assertTrue(out["quote_time"])

    def test_live_quote_at_trigger_upgrades_armed_to_triggered(self):
        entry = _entry()
        with mock.patch.object(watchlist, "fetch_live_price", return_value=89.0), \
             mock.patch("yfinance.Ticker") as tk:
            tk.return_value.history.return_value = None
            out = watchlist.refresh_entry(entry, today=date(2026, 7, 13))
        self.assertEqual(out["status"], "TRIGGERED")
        self.assertEqual(out["hit_date"], "2026-07-13")

    def test_pending_entry_is_not_triggered_by_a_live_quote(self):
        entry = _entry(earnings_date="2026-07-30")
        with mock.patch.object(watchlist, "fetch_live_price", return_value=50.0):
            out = watchlist.refresh_entry(entry, today=date(2026, 7, 13))
        self.assertEqual(out["status"], "PENDING")
        self.assertEqual(out["current_price"], 50.0)

    def test_no_quote_falls_back_to_close(self):
        entry = _entry()
        with mock.patch.object(watchlist, "fetch_live_price", return_value=None), \
             mock.patch("yfinance.Ticker") as tk:
            tk.return_value.history.return_value = None
            out = watchlist.refresh_entry(entry, today=date(2026, 7, 13))
        self.assertIsNone(out["live_price"])
        self.assertIn(out["price_source"], (None, "close"))

    def test_quote_is_ttl_cached(self):
        fake = mock.Mock()
        fake.fast_info.last_price = 101.0
        with mock.patch("yfinance.Ticker", return_value=fake) as tk:
            self.assertEqual(watchlist.fetch_live_price("AAA"), 101.0)
            self.assertEqual(watchlist.fetch_live_price("aaa"), 101.0)   # cache hit, case-insensitive
        self.assertEqual(tk.call_count, 1)

    def test_quote_failure_is_swallowed(self):
        with mock.patch("yfinance.Ticker", side_effect=RuntimeError("network down")):
            self.assertIsNone(watchlist.fetch_live_price("AAA"))


if __name__ == "__main__":
    unittest.main()
