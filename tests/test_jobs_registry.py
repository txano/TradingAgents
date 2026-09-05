"""Unit tests for the durable job registry. No network, no real processes spawned."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from cli import jobs_registry as jr


def _dead_pid() -> int:
    """A PID that is almost certainly not running (max_pid + a bit)."""
    return 4_194_303


@pytest.mark.unit
class RegistryBasicsTests(unittest.TestCase):
    def test_register_load_finish_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            rec = jr.register("screen", label="Screening", reports_root=td)
            self.assertEqual(rec["status"], jr.RUNNING)
            self.assertEqual(rec["pid"], os.getpid())

            loaded = jr.load(td)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["status"], jr.RUNNING)   # our own pid is alive

            jr.finish(rec["id"], reports_root=td)
            self.assertEqual(jr.load(td)[0]["status"], jr.DONE)
            self.assertEqual(jr.active(td), [])

    def test_registry_file_lands_in_reports_root(self):
        with tempfile.TemporaryDirectory() as td:
            jr.register("screen", reports_root=td)
            self.assertTrue((Path(td) / "jobs.json").exists())

    def test_update_merges_fields(self):
        with tempfile.TemporaryDirectory() as td:
            rec = jr.register("screen", reports_root=td)
            jr.update(rec["id"], reports_root=td, total=42, label="Screening run_x")
            got = jr.load(td)[0]
            self.assertEqual(got["total"], 42)
            self.assertEqual(got["label"], "Screening run_x")

    def test_update_of_unknown_id_is_a_noop(self):
        with tempfile.TemporaryDirectory() as td:
            jr.register("screen", reports_root=td)
            jr.update("nope", reports_root=td, total=1)
            self.assertIsNone(jr.load(td)[0]["total"])

    def test_re_registering_same_id_replaces(self):
        with tempfile.TemporaryDirectory() as td:
            jr.register("screen", job_id="dup", reports_root=td)
            jr.register("resume", job_id="dup", reports_root=td)
            records = jr.load(td)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["type"], "resume")

    def test_missing_or_corrupt_file_reads_empty(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(jr.load(td), [])
            (Path(td) / "jobs.json").write_text("{not json", encoding="utf-8")
            self.assertEqual(jr.load(td), [])


@pytest.mark.unit
class LivenessTests(unittest.TestCase):
    """A record claiming to run is only believed if its process exists."""

    def test_dead_pid_reads_back_as_interrupted(self):
        with tempfile.TemporaryDirectory() as td:
            jr.register("screen", pid=_dead_pid(), reports_root=td)
            self.assertEqual(jr.load(td)[0]["status"], jr.INTERRUPTED)
            self.assertEqual(jr.active(td), [])

    def test_finished_records_are_not_reinterpreted(self):
        with tempfile.TemporaryDirectory() as td:
            rec = jr.register("screen", pid=_dead_pid(), reports_root=td)
            jr.finish(rec["id"], reports_root=td)
            self.assertEqual(jr.load(td)[0]["status"], jr.DONE)

    def test_reap_persists_the_inferred_state(self):
        with tempfile.TemporaryDirectory() as td:
            jr.register("screen", pid=_dead_pid(), reports_root=td)
            self.assertEqual(jr.reap(td), 1)
            raw = json.loads((Path(td) / "jobs.json").read_text())
            self.assertEqual(raw[0]["status"], jr.INTERRUPTED)
            self.assertEqual(jr.reap(td), 0)          # idempotent

    def test_unknown_pid_is_left_alone(self):
        with tempfile.TemporaryDirectory() as td:
            jr.register("screen", pid=None, reports_root=td)
            jr.update(jr.load(td)[0]["id"], reports_root=td, pid=None)
            self.assertEqual(jr.load(td)[0]["status"], jr.RUNNING)


@pytest.mark.unit
class ProgressTests(unittest.TestCase):
    """Progress is recounted from disk, so a silent job still reports honestly."""

    def _run_dir(self, root: Path, done: int, empty: int = 0) -> Path:
        run = root / "run_x"
        run.mkdir(parents=True)
        for i in range(done):
            d = run / f"T{i}"
            d.mkdir()
            (d / "earnings_brief.md").write_text("brief", encoding="utf-8")
        for i in range(empty):
            (run / f"E{i}").mkdir()
        return run

    def test_progress_counts_briefs_not_folders(self):
        with tempfile.TemporaryDirectory() as td:
            run = self._run_dir(Path(td), done=3, empty=2)
            jr.register("screen", run_dir=run, total=10, reports_root=td)
            self.assertEqual(jr.load(td)[0]["progress"], {"done": 3, "total": 10, "pct": 30})

    def test_progress_without_total_has_no_pct(self):
        with tempfile.TemporaryDirectory() as td:
            run = self._run_dir(Path(td), done=2)
            jr.register("screen", run_dir=run, reports_root=td)
            self.assertEqual(jr.load(td)[0]["progress"], {"done": 2, "total": None, "pct": None})

    def test_no_run_dir_means_no_progress(self):
        with tempfile.TemporaryDirectory() as td:
            jr.register("improve", reports_root=td)
            self.assertIsNone(jr.load(td)[0]["progress"])

    def test_vanished_run_dir_degrades_quietly(self):
        with tempfile.TemporaryDirectory() as td:
            jr.register("screen", run_dir=Path(td) / "gone", total=5, reports_root=td)
            self.assertIsNone(jr.load(td)[0]["progress"])


@pytest.mark.unit
class PruneTests(unittest.TestCase):
    def test_old_finished_records_are_dropped_but_running_kept(self):
        with tempfile.TemporaryDirectory() as td:
            old = (datetime.now(timezone.utc) - timedelta(days=jr.KEEP_FINISHED_DAYS + 2)).isoformat()
            (Path(td) / "jobs.json").write_text(json.dumps([
                {"id": "old", "type": "screen", "status": "done", "finished_at": old},
                {"id": "run", "type": "screen", "status": "running", "pid": os.getpid()},
            ]), encoding="utf-8")
            jr.register("resume", job_id="new", reports_root=td)      # register prunes
            ids = {r["id"] for r in jr.load(td)}
            self.assertEqual(ids, {"run", "new"})


if __name__ == "__main__":
    unittest.main()
