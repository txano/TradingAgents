"""Watchlist fetch caching + the server's warm snapshot. No network.

The dashboard looked stuttery because the payload it was served could not know
which rows were TRIGGERED — that needs a live quote — so rows flipped a second
after paint. Two things fix it, and both are load-bearing:

* bars are TTL-cached and fetched concurrently (they were the dominant cost:
  one sequential yfinance history call per entry), so a refresh is cheap; and
* the server keeps one warm snapshot, so the embedded payload is already
  live-accurate and no request ever blocks on a fetch.
"""

import threading
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import pytest

from tradingagents.allocation import watchlist


@pytest.fixture(autouse=True)
def _clean_caches():
    watchlist.clear_caches()
    yield
    watchlist.clear_caches()


class _Bar:
    """Stand-in for a yfinance history frame."""

    def __init__(self, rows):
        self._rows = rows
        self.empty = not rows

    def iterrows(self):
        for d, low, close in self._rows:
            yield mock.Mock(date=lambda d=d: d), {"Low": low, "Close": close}


def _hist(rows):
    tk = mock.Mock()
    tk.return_value.history.return_value = _Bar(rows)
    return tk


@pytest.mark.unit
class BarCacheTests(unittest.TestCase):
    def test_second_call_does_not_hit_the_network(self):
        tk = _hist([(date(2026, 7, 10), 88.0, 91.0)])
        with mock.patch("yfinance.Ticker", tk):
            first = watchlist.fetch_bars("AAA", "2026-07-09")
            second = watchlist.fetch_bars("AAA", "2026-07-09")
        self.assertEqual(first, second)
        self.assertEqual(tk.return_value.history.call_count, 1)

    def test_a_different_start_is_a_different_key(self):
        tk = _hist([(date(2026, 7, 10), 88.0, 91.0)])
        with mock.patch("yfinance.Ticker", tk):
            watchlist.fetch_bars("AAA", "2026-07-09")
            watchlist.fetch_bars("AAA", "2026-07-01")
        self.assertEqual(tk.return_value.history.call_count, 2)

    def test_expired_entry_refetches(self):
        tk = _hist([(date(2026, 7, 10), 88.0, 91.0)])
        with mock.patch("yfinance.Ticker", tk):
            watchlist.fetch_bars("AAA", "2026-07-09", ttl=0)
            watchlist.fetch_bars("AAA", "2026-07-09", ttl=0)
        self.assertEqual(tk.return_value.history.call_count, 2)

    def test_failure_is_swallowed_and_not_cached(self):
        tk = mock.Mock()
        tk.return_value.history.side_effect = RuntimeError("yahoo down")
        with mock.patch("yfinance.Ticker", tk):
            self.assertIsNone(watchlist.fetch_bars("AAA", "2026-07-09"))
            self.assertIsNone(watchlist.fetch_bars("AAA", "2026-07-09"))
        self.assertEqual(tk.return_value.history.call_count, 2)

    def test_blank_inputs_never_reach_the_network(self):
        tk = _hist([])
        with mock.patch("yfinance.Ticker", tk):
            self.assertIsNone(watchlist.fetch_bars("", "2026-07-09"))
            self.assertIsNone(watchlist.fetch_bars("AAA", ""))
        tk.assert_not_called()

    def test_clear_caches_drops_both(self):
        watchlist._QUOTE_CACHE["AAA"] = (time.time(), 10.0)
        watchlist._BARS_CACHE[("AAA", "x")] = (time.time(), [])
        watchlist.clear_caches()
        self.assertFalse(watchlist._QUOTE_CACHE)
        self.assertFalse(watchlist._BARS_CACHE)


@pytest.mark.unit
class CollectFetchScopeTests(unittest.TestCase):
    """Only rows that get a live refresh are worth a round-trip."""

    def _run(self, tmp, entries):
        run = Path(tmp) / "earnings" / "earnings_2026-07-09_20260709_120000"
        run.mkdir(parents=True)
        watchlist.save_watchlist(entries, run)
        return run

    def _entry(self, ticker, earnings_date):
        return {
            "ticker": ticker, "trigger_price": 90.0, "watch_amount": 1000,
            "spot_at_screen": 100.0, "earnings_date": earnings_date,
            "expiry_sessions": 5, "thesis": "t",
        }

    def test_dismissed_rows_are_not_fetched(self):
        import tempfile
        today = date.today()
        recent = (today - timedelta(days=2)).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            run = self._run(tmp, [self._entry("AAA", recent), self._entry("BBB", recent)])
            watchlist.set_entry_state(run.name, "BBB", "dismissed", reports_root=Path(tmp))
            with mock.patch.object(watchlist, "fetch_live_price", return_value=95.0) as q, \
                 mock.patch.object(watchlist, "fetch_bars", return_value=None):
                watchlist.collect_watchlists(Path(tmp))
        asked = {c.args[0] for c in q.call_args_list}
        self.assertIn("AAA", asked)
        self.assertNotIn("BBB", asked)

    def test_stale_rows_are_not_fetched(self):
        import tempfile
        old = (date.today() - timedelta(days=400)).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp, [self._entry("OLD", old)])
            with mock.patch.object(watchlist, "fetch_live_price", return_value=95.0) as q, \
                 mock.patch.object(watchlist, "fetch_bars", return_value=None):
                out = watchlist.collect_watchlists(Path(tmp))
        q.assert_not_called()
        self.assertEqual(out[0]["status"], "EXPIRED")

    def test_refresh_false_fetches_nothing(self):
        import tempfile
        recent = (date.today() - timedelta(days=2)).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp, [self._entry("AAA", recent)])
            with mock.patch.object(watchlist, "fetch_live_price") as q, \
                 mock.patch.object(watchlist, "fetch_bars") as b:
                watchlist.collect_watchlists(Path(tmp), refresh=False)
        q.assert_not_called()
        b.assert_not_called()

    def test_fetches_run_concurrently(self):
        """Sequential, this was the whole cost. Serial execution must fail here."""
        import tempfile
        recent = (date.today() - timedelta(days=2)).isoformat()
        tickers = [f"T{i}" for i in range(8)]
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp, [self._entry(t, recent) for t in tickers])
            lock, live, peak = threading.Lock(), [0], [0]

            def slow(result):
                def call(*a, **kw):
                    with lock:
                        live[0] += 1
                        peak[0] = max(peak[0], live[0])
                    time.sleep(0.05)    # long enough that serial calls can't overlap
                    with lock:
                        live[0] -= 1
                    return result
                return call

            with mock.patch.object(watchlist, "fetch_live_price", side_effect=slow(95.0)), \
                 mock.patch.object(watchlist, "fetch_bars", side_effect=slow(None)):
                watchlist.collect_watchlists(Path(tmp))
        self.assertGreater(peak[0], 1, "warm-up fetches ran serially")


@pytest.mark.unit
class ServerSnapshotTests(unittest.TestCase):
    """Requests take what is cached and revalidate behind them."""

    def setUp(self):
        from cli import server
        self.server = server
        with server._WL_LOCK:
            server._WL_SNAP["data"], server._WL_SNAP["at"] = None, 0.0

    def tearDown(self):
        with self.server._WL_LOCK:
            self.server._WL_SNAP["data"], self.server._WL_SNAP["at"] = None, 0.0

    def test_cold_call_can_decline_to_block(self):
        with mock.patch.object(self.server, "_wl_compute", side_effect=AssertionError("blocked")), \
             mock.patch.object(self.server, "_wl_refresh_bg") as bg:
            self.assertEqual(self.server.watchlist_snapshot(block_if_cold=False), [])
        bg.assert_called_once()

    def test_warm_call_never_recomputes(self):
        rows = [{"ticker": "AAA", "status": "TRIGGERED"}]
        with self.server._WL_LOCK:
            self.server._WL_SNAP["data"], self.server._WL_SNAP["at"] = rows, time.time()
        with mock.patch.object(self.server, "_wl_compute", side_effect=AssertionError("recomputed")):
            self.assertEqual(self.server.watchlist_snapshot(), rows)

    def test_stale_snapshot_is_served_and_revalidated(self):
        rows = [{"ticker": "AAA", "status": "ARMED"}]
        with self.server._WL_LOCK:
            self.server._WL_SNAP["data"] = rows
            self.server._WL_SNAP["at"] = time.time() - self.server._WL_TTL_SECONDS - 1
        with mock.patch.object(self.server, "_wl_refresh_bg") as bg:
            self.assertEqual(self.server.watchlist_snapshot(), rows)   # stale, immediately
        bg.assert_called_once()                                        # …and refreshed behind

    def test_first_cold_call_computes_and_publishes(self):
        rows = [{"ticker": "AAA", "status": "TRIGGERED"}]
        with mock.patch.object(self.server, "_wl_compute", return_value=rows) as comp:
            self.assertEqual(self.server.watchlist_snapshot(), rows)
            self.assertEqual(self.server.watchlist_snapshot(), rows)
        comp.assert_called_once()

    def test_a_failing_refresh_does_not_raise(self):
        with mock.patch.object(self.server, "_wl_compute", side_effect=RuntimeError("yahoo down")):
            self.assertEqual(self.server.watchlist_snapshot(), [])

    def test_background_refresh_is_single_flight(self):
        started, release = threading.Semaphore(0), threading.Event()

        def slow():
            started.release()
            release.wait(5)
            return []

        with mock.patch.object(self.server, "_wl_compute", side_effect=slow):
            self.server._wl_refresh_bg()
            self.assertTrue(started.acquire(timeout=5))
            self.server._wl_refresh_bg()          # must not start a second one
            self.assertFalse(started.acquire(timeout=0.2))
            release.set()
        time.sleep(0.2)


if __name__ == "__main__":
    unittest.main()
