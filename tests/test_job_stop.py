"""Stopping a job. No network.

The load-bearing constraint: dashboard jobs run as *threads inside the server
process*, so their recorded PID is the server's own. A "stop" that signals that
PID takes the dashboard down with the job. Stopping is therefore cooperative —
queued work is never started, in-flight work finishes — and only a job in some
*other* process may be signalled.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

from cli import jobs_registry as jr


@pytest.fixture(autouse=True)
def _no_local_cancel_leak():
    jr._CANCEL_LOCAL.clear()
    yield
    jr._CANCEL_LOCAL.clear()


@pytest.mark.unit
class CancelStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _register(self, **kw):
        return jr.register("screen", reports_root=self.root, **kw)

    def test_request_cancel_marks_cancelling(self):
        rec = self._register(job_id="a1")
        self.assertEqual(rec["status"], jr.RUNNING)
        out = jr.request_cancel("a1", reports_root=self.root)
        self.assertEqual(out["status"], jr.CANCELLING)
        self.assertIn("cancel_requested_at", out)

    def test_is_cancelled_sees_it(self):
        self._register(job_id="a1")
        self.assertFalse(jr.is_cancelled("a1", reports_root=self.root))
        jr.request_cancel("a1", reports_root=self.root)
        self.assertTrue(jr.is_cancelled("a1", reports_root=self.root))

    def test_is_cancelled_reads_across_processes(self):
        """The file is the cross-process signal — not just the in-memory set."""
        self._register(job_id="a1")
        jr.request_cancel("a1", reports_root=self.root)
        jr._CANCEL_LOCAL.clear()                    # pretend we are another process
        self.assertTrue(jr.is_cancelled("a1", reports_root=self.root))

    def test_is_cancelled_is_false_for_unknown_and_blank(self):
        self.assertFalse(jr.is_cancelled(None, reports_root=self.root))
        self.assertFalse(jr.is_cancelled("", reports_root=self.root))
        self.assertFalse(jr.is_cancelled("nope", reports_root=self.root))

    def test_cancelling_a_finished_job_does_not_reopen_it(self):
        self._register(job_id="a1")
        jr.finish("a1", jr.DONE, reports_root=self.root)
        out = jr.request_cancel("a1", reports_root=self.root)
        self.assertEqual(out["status"], jr.DONE)
        rec = next(r for r in jr.load(self.root) if r["id"] == "a1")
        self.assertEqual(rec["status"], jr.DONE)

    def test_cancelling_job_with_dead_pid_reads_back_cancelled(self):
        self._register(job_id="a1", pid=999_999)
        jr.request_cancel("a1", reports_root=self.root)
        with mock.patch.object(jr, "_alive", return_value=False):
            rec = next(r for r in jr.load(self.root) if r["id"] == "a1")
        self.assertEqual(rec["status"], jr.CANCELLED)

    def test_running_job_with_dead_pid_still_reads_interrupted(self):
        """The distinction drives whether the UI offers a resume."""
        self._register(job_id="a1", pid=999_999)
        with mock.patch.object(jr, "_alive", return_value=False):
            rec = next(r for r in jr.load(self.root) if r["id"] == "a1")
        self.assertEqual(rec["status"], jr.INTERRUPTED)

    def test_cancelling_survives_pruning(self):
        self._register(job_id="a1")
        jr.request_cancel("a1", reports_root=self.root)
        kept = jr._prune(jr._read(self.root))
        self.assertEqual([r["id"] for r in kept], ["a1"])

    def test_active_includes_stopping_work(self):
        self._register(job_id="a1", pid=os.getpid())
        jr.request_cancel("a1", reports_root=self.root)
        self.assertEqual([r["id"] for r in jr.active(self.root)], ["a1"])

    def test_reap_settles_a_cancelling_job_as_cancelled(self):
        self._register(job_id="a1", pid=999_999)
        jr.request_cancel("a1", reports_root=self.root)
        with mock.patch.object(jr, "_alive", return_value=False):
            self.assertEqual(jr.reap(self.root), 1)
        rec = next(r for r in jr._read(self.root) if r["id"] == "a1")
        self.assertEqual(rec["status"], jr.CANCELLED)


def _signals(kill_mock):
    """Terminating signals only — `os.kill(pid, 0)` is a liveness probe."""
    return [c.args for c in kill_mock.call_args_list if len(c.args) > 1 and c.args[1] != 0]


@pytest.mark.unit
class StopEndpointTests(unittest.TestCase):
    """`/api/jobs/{id}/stop` must never signal its own process."""

    def setUp(self):
        from cli import server
        self.server = server
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # The endpoint hard-codes Path("reports"); run it against a temp root.
        self._cwd = os.getcwd()
        os.chdir(self.tmp.name)
        (self.root / "reports").mkdir(exist_ok=True)
        with server._JOBS_LOCK:
            server._jobs.clear()

    def tearDown(self):
        os.chdir(self._cwd)
        with self.server._JOBS_LOCK:
            self.server._jobs.clear()
        self.tmp.cleanup()

    def _job(self, job_id, pid, in_memory=True):
        jr.register("screen", job_id=job_id, pid=pid, reports_root=Path("reports"))
        if in_memory:
            import queue
            with self.server._JOBS_LOCK:
                self.server._jobs[job_id] = {
                    "id": job_id, "type": "screen", "params": {},
                    "status": "running", "log": [], "queue": queue.Queue(),
                }

    def test_own_process_job_is_never_signalled(self):
        """The whole point: SIGTERM here would kill the dashboard."""
        self._job("a1", os.getpid())
        with mock.patch("os.kill") as kill:
            out = self.server.stop_job("a1")
        self.assertEqual(_signals(kill), [])
        self.assertEqual(out["status"], jr.CANCELLING)
        self.assertTrue(jr.is_cancelled("a1", reports_root=Path("reports")))

    def test_external_live_process_gets_sigterm(self):
        import signal
        self._job("a1", 424242, in_memory=False)
        with mock.patch.object(jr, "_alive", return_value=True), \
             mock.patch("os.kill") as kill:
            out = self.server.stop_job("a1")
        kill.assert_called_once_with(424242, signal.SIGTERM)
        self.assertEqual(out["status"], jr.CANCELLING)

    def test_force_escalates_to_sigkill(self):
        import signal
        self._job("a1", 424242, in_memory=False)
        with mock.patch.object(jr, "_alive", return_value=True), \
             mock.patch("os.kill") as kill:
            self.server.stop_job("a1", force=True)
        kill.assert_called_once_with(424242, signal.SIGKILL)

    def test_dead_process_is_settled_not_signalled(self):
        """Already gone: nothing to stop, but the record must stop saying 'running'."""
        self._job("a1", 424242, in_memory=False)
        with mock.patch.object(jr, "_alive", return_value=False), \
             mock.patch("os.kill") as kill:
            out = self.server.stop_job("a1")
        self.assertEqual(_signals(kill), [])
        self.assertEqual(out["status"], jr.INTERRUPTED)
        stored = next(r for r in jr._read(Path("reports")) if r["id"] == "a1")
        self.assertEqual(stored["status"], jr.INTERRUPTED)   # persisted, not just derived

    def test_our_pid_but_no_live_thread_is_cleared(self):
        """A record from a previous server that was reassigned this PID."""
        self._job("a1", os.getpid(), in_memory=False)
        with mock.patch("os.kill") as kill:
            out = self.server.stop_job("a1")
        self.assertEqual(_signals(kill), [])
        self.assertEqual(out["status"], jr.CANCELLED)

    def test_unknown_job_404s(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self.server.stop_job("nope")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_already_finished_job_is_reported_not_restopped(self):
        self._job("a1", os.getpid(), in_memory=False)
        jr.finish("a1", jr.DONE, reports_root=Path("reports"))
        with mock.patch("os.kill") as kill:
            out = self.server.stop_job("a1")
        kill.assert_not_called()
        self.assertEqual(out["status"], jr.DONE)

    def test_finish_records_cancelled_when_a_stop_was_requested(self):
        self._job("a1", os.getpid())
        self.server.stop_job("a1")
        self.server._finish("a1", True)
        rec = next(r for r in jr._read(Path("reports")) if r["id"] == "a1")
        self.assertEqual(rec["status"], jr.CANCELLED)

    def test_normal_finish_is_still_done(self):
        self._job("a1", os.getpid())
        self.server._finish("a1", True)
        rec = next(r for r in jr._read(Path("reports")) if r["id"] == "a1")
        self.assertEqual(rec["status"], jr.DONE)


@pytest.mark.unit
class CooperativeSkipTests(unittest.TestCase):
    """Cancelling stops *queued* work; it cannot interrupt a call in flight."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_resume_skips_queued_tickers_and_does_not_allocate(self):
        from cli.commands import resume as R

        run_dir = self.root / "earnings" / "earnings_2026-08-14_20260813_200111"
        run_dir.mkdir(parents=True)
        plan = {
            "name": run_dir.name, "missing": ["AAA", "BBB", "CCC"], "done": [],
            "total": 3, "trade_date": "2026-08-14", "metadata": {},
            "universe_source": "metadata", "has_allocation": False,
        }
        screened = []

        def fake_screen(ticker, *a, **kw):
            screened.append(ticker)
            # The first ticker's own work triggers the stop, as a real user click would
            jr.request_cancel(rid[0], reports_root=self.root)
            return {"ticker": ticker, "signal": "BUY", "total_score": 1}

        rid = [None]
        real_register = jr.register

        def capture(*a, **kw):
            rec = real_register(*a, **kw)
            rid[0] = rec["id"]
            return rec

        with mock.patch.object(R, "plan_resume", return_value=plan), \
             mock.patch.object(R, "build_config", return_value={
                 "llm_provider": "deepseek", "quick_think_llm": "q", "deep_think_llm": "d"}), \
             mock.patch("cli.commands.common.gather_api_keys", return_value=["k"]), \
             mock.patch("cli.commands.screen.screen_ticker", side_effect=fake_screen), \
             mock.patch("cli.commands.screen.run_allocation") as alloc, \
             mock.patch("cli.commands.common._fetch_sector", return_value="Tech"), \
             mock.patch("cli.commands.common.parse_brief_scores",
                        return_value={"ticker": "AAA", "total_score": 1}), \
             mock.patch.object(jr, "register", side_effect=capture):
            summary = R.resume_run(run_dir, workers=1, log=lambda m: None,
                                   reports_root=self.root)

        self.assertTrue(summary.get("cancelled"))
        alloc.assert_not_called()                     # no 11-call council on a partial run
        self.assertLess(len(screened), 3)             # queued tickers never started
        rec = next(r for r in jr._read(self.root) if r["id"] == rid[0])
        self.assertEqual(rec["status"], jr.CANCELLED)


if __name__ == "__main__":
    unittest.main()
