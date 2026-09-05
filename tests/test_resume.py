"""Unit tests for resuming an interrupted screening run. No network, no LLM calls."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

from cli.commands import resume as rz

BRIEF = """# Brief

```json
{"earnings_date": "2026-08-10", "beat_score": 2, "guidance_score": 1,
 "setup_score": 0, "total_score": 3, "signal": "BUY", "confidence": "High",
 "one_liner": "looks fine"}
```
"""


def make_run(root: Path, name: str, done=(), empty=(), meta=None, allocation=False) -> Path:
    run = root / name
    run.mkdir(parents=True)
    for t in done:
        d = run / t
        d.mkdir()
        (d / "earnings_brief.md").write_text(BRIEF, encoding="utf-8")
    for t in empty:
        (run / t).mkdir()
    if meta is not None:
        (run / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    if allocation:
        (run / "allocation.md").write_text("# alloc", encoding="utf-8")
    return run


def patch_calendar(entries, date="2026-08-10"):
    """Point the module at a temp calendar file holding `entries` tickers."""
    tmp = Path(tempfile.mkdtemp()) / "calendar.json"
    tmp.write_text(json.dumps({date: {"entries": [{"ticker": t} for t in entries]}}), encoding="utf-8")
    return mock.patch.object(rz, "CALENDAR_PATH", tmp)


@pytest.mark.unit
class PlanResumeTests(unittest.TestCase):
    META = {"earnings_date": "2026-08-10", "trade_date": "2026-08-10",
            "provider": "deepseek", "quick_model": "q", "deep_model": "d", "depth": 3}

    def test_calendar_universe_drives_missing(self):
        """Opt-in since 2026-08-12: the calendar is a superset, not the default."""
        with tempfile.TemporaryDirectory() as td, patch_calendar(["AAA", "BBB", "CCC", "DDD"]):
            run = make_run(Path(td), "earnings_2026-08-10_1", done=["AAA", "BBB"], meta=self.META)
            plan = rz.plan_resume(run, universe="calendar")
        self.assertEqual(plan["universe_source"], "calendar")
        self.assertEqual(plan["total"], 4)
        self.assertEqual(plan["done"], ["AAA", "BBB"])
        self.assertEqual(plan["missing"], ["CCC", "DDD"])

    def test_started_but_empty_folders_come_first(self):
        """Gaps mid-run are the earliest work, so they're retried before new tickers."""
        with tempfile.TemporaryDirectory() as td, patch_calendar(["AAA", "BBB", "CCC", "DDD"]):
            run = make_run(Path(td), "earnings_2026-08-10_1",
                           done=["AAA"], empty=["CCC"], meta=self.META)
            plan = rz.plan_resume(run, universe="calendar")
        self.assertEqual(plan["missing"], ["CCC", "BBB", "DDD"])

    def test_falls_back_to_folder_universe_without_calendar(self):
        with tempfile.TemporaryDirectory() as td, patch_calendar([], date="1999-01-01"):
            run = make_run(Path(td), "screening_2026-05-01_1", done=["AAA"], empty=["BBB"])
            plan = rz.plan_resume(run)
        self.assertEqual(plan["universe_source"], "folder")
        self.assertEqual(plan["missing"], ["BBB"])
        self.assertEqual(plan["total"], 2)

    def test_complete_run_has_nothing_missing(self):
        with tempfile.TemporaryDirectory() as td, patch_calendar(["AAA", "BBB"]):
            run = make_run(Path(td), "earnings_2026-08-10_1",
                           done=["AAA", "BBB"], meta=self.META, allocation=True)
            plan = rz.plan_resume(run)
        self.assertEqual(plan["missing"], [])
        self.assertTrue(plan["has_allocation"])

    def test_dates_fall_back_to_the_folder_name(self):
        with tempfile.TemporaryDirectory() as td, patch_calendar([], date="1999-01-01"):
            run = make_run(Path(td), "earnings_2026-08-10_20260810_181535", done=["AAA"])
            plan = rz.plan_resume(run)
        self.assertEqual(plan["trade_date"], "2026-08-10")
        self.assertEqual(plan["earnings_date"], "2026-08-10")

    def test_resumable_runs_skips_complete_ones(self):
        with tempfile.TemporaryDirectory() as td, patch_calendar(["AAA", "BBB"]):
            base = Path(td) / "earnings"
            make_run(base, "earnings_2026-08-10_a", done=["AAA", "BBB"],
                     meta=self.META, allocation=True)
            make_run(base, "earnings_2026-08-10_b", done=["AAA"], empty=["BBB"],
                     meta=self.META, allocation=True)
            names = {r["name"] for r in rz.resumable_runs(td)}
        self.assertEqual(names, {"earnings_2026-08-10_b"})

    def test_run_missing_only_its_allocation_is_resumable(self):
        with tempfile.TemporaryDirectory() as td, patch_calendar(["AAA"]):
            base = Path(td) / "earnings"
            make_run(base, "earnings_2026-08-10_c", done=["AAA"], meta=self.META)
            runs = rz.resumable_runs(td)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["missing"], 0)
        self.assertFalse(runs[0]["has_allocation"])


@pytest.mark.unit
class UniverseSourceTests(unittest.TestCase):
    """Which tickers a resume believes it owes. Guessing high spends real money."""

    META = dict(PlanResumeTests.META)

    def test_submitted_list_wins_over_the_calendar(self):
        with tempfile.TemporaryDirectory() as td, patch_calendar(["AAA", "BBB", "CCC", "DDD"]):
            meta = {**self.META, "tickers": ["AAA", "BBB"]}
            run = make_run(Path(td), "earnings_2026-08-10_1", done=["AAA"], meta=meta)
            plan = rz.plan_resume(run)
        self.assertEqual(plan["universe_source"], "metadata")
        self.assertEqual(plan["total"], 2)
        self.assertEqual(plan["missing"], ["BBB"])
        self.assertEqual(plan["calendar_extra"], ["CCC", "DDD"])   # reported, not applied

    def test_a_filtered_run_does_not_pull_in_the_whole_calendar(self):
        """The 2026-08-12 case: 84 submitted of a 168-ticker day planned 128."""
        with tempfile.TemporaryDirectory() as td, patch_calendar([f"T{i}" for i in range(168)]):
            run = make_run(Path(td), "earnings_2026-08-10_1",
                           done=[f"T{i}" for i in range(40)],
                           empty=[f"T{i}" for i in range(40, 84)], meta=self.META)
            plan = rz.plan_resume(run)
        self.assertEqual(plan["universe_source"], "folder")
        self.assertEqual(plan["total"], 84)
        self.assertEqual(len(plan["missing"]), 44)
        self.assertEqual(len(plan["calendar_extra"]), 84)

    def test_calendar_is_available_on_request(self):
        with tempfile.TemporaryDirectory() as td, patch_calendar(["AAA", "BBB", "CCC"]):
            meta = {**self.META, "tickers": ["AAA"]}
            run = make_run(Path(td), "earnings_2026-08-10_1", done=["AAA"], meta=meta)
            plan = rz.plan_resume(run, universe="calendar")
        self.assertEqual(plan["universe_source"], "calendar")
        self.assertEqual(plan["missing"], ["BBB", "CCC"])

    def test_calendar_used_when_nothing_started_yet(self):
        with tempfile.TemporaryDirectory() as td, patch_calendar(["AAA", "BBB"]):
            run = make_run(Path(td), "earnings_2026-08-10_1", meta=self.META)
            plan = rz.plan_resume(run)
        self.assertEqual(plan["universe_source"], "calendar")
        self.assertEqual(plan["missing"], ["AAA", "BBB"])

    def test_resume_run_honours_the_universe_argument(self):
        with tempfile.TemporaryDirectory() as td, patch_calendar(["AAA", "BBB", "CCC"]):
            meta = {**self.META, "tickers": ["AAA", "BBB"]}
            run = make_run(Path(td), "earnings_2026-08-10_1", done=["AAA"], meta=meta)
            seen = []

            def fake_screen(ticker, trade_date, ticker_dir, cfg, **kw):
                seen.append(ticker)
                Path(ticker_dir).mkdir(parents=True, exist_ok=True)
                (Path(ticker_dir) / "earnings_brief.md").write_text(BRIEF, encoding="utf-8")
                return {"ticker": ticker, "signal": "BUY", "total_score": 1}

            with mock.patch("cli.commands.screen.screen_ticker", side_effect=fake_screen), \
                 mock.patch("cli.commands.common._fetch_sector", return_value="Tech"), \
                 mock.patch("cli.commands.common.gather_api_keys", return_value=["k"]):
                rz.resume_run(run, allocate=False, log=lambda m: None, reports_root=td)
        self.assertEqual(seen, ["BBB"])          # not CCC, which the run never asked for


@pytest.mark.unit
class BuildConfigTests(unittest.TestCase):
    def test_metadata_reproduces_the_original_run_config(self):
        cfg = rz.build_config({"provider": "deepseek", "quick_model": "q",
                               "deep_model": "d", "depth": 3})
        self.assertEqual(cfg["llm_provider"], "deepseek")
        self.assertEqual(cfg["quick_think_llm"], "q")
        self.assertEqual(cfg["deep_think_llm"], "d")
        self.assertEqual(cfg["max_debate_rounds"], 3)
        self.assertEqual(cfg["max_risk_discuss_rounds"], 3)

    def test_empty_metadata_leaves_defaults_intact(self):
        from tradingagents.default_config import DEFAULT_CONFIG
        cfg = rz.build_config({})
        self.assertEqual(cfg["llm_provider"], DEFAULT_CONFIG["llm_provider"])

    def test_overrides_beat_metadata(self):
        cfg = rz.build_config({"provider": "deepseek"}, {"llm_provider": "openai"})
        self.assertEqual(cfg["llm_provider"], "openai")


@pytest.mark.unit
class ResumeRunTests(unittest.TestCase):
    # A real run records what it submitted (server `_run_screen` writes `tickers`),
    # which is what a resume owes — see UniverseSourceTests.
    META = {"earnings_date": "2026-08-10", "trade_date": "2026-08-10",
            "provider": "deepseek", "quick_model": "q", "deep_model": "d", "depth": 3,
            "tickers": ["AAA", "BBB", "CCC"]}

    def test_screens_only_missing_then_rebuilds_table(self):
        with tempfile.TemporaryDirectory() as td, patch_calendar(["AAA", "BBB", "CCC"]):
            run = make_run(Path(td), "earnings_2026-08-10_1", done=["AAA"], meta=self.META)
            screened = []

            def fake_screen(ticker, trade_date, ticker_dir, cfg, **kw):
                screened.append(ticker)
                Path(ticker_dir).mkdir(parents=True, exist_ok=True)
                (Path(ticker_dir) / "earnings_brief.md").write_text(BRIEF, encoding="utf-8")
                return {"ticker": ticker, "signal": "BUY", "total_score": 3}

            with mock.patch("cli.commands.screen.screen_ticker", side_effect=fake_screen), \
                 mock.patch("cli.commands.common._fetch_sector", return_value="Tech"), \
                 mock.patch("cli.commands.common.gather_api_keys", return_value=["k"]):
                summary = rz.resume_run(run, allocate=False, workers=2,
                                        log=lambda m: None, reports_root=td)
            table_written = (run / "screening_table.md").exists()

        self.assertEqual(sorted(screened), ["BBB", "CCC"])       # AAA was already done
        self.assertEqual(summary["screened"], 2)
        self.assertEqual(summary["errors"], 0)
        self.assertFalse(summary["allocated"])
        self.assertTrue(table_written)
        self.assertEqual(summary["table_rows"], 3)

    def test_nothing_missing_still_rebuilds_the_table(self):
        with tempfile.TemporaryDirectory() as td, patch_calendar(["AAA"]):
            meta = {**self.META, "tickers": ["AAA"]}
            run = make_run(Path(td), "earnings_2026-08-10_1", done=["AAA"], meta=meta)
            # screen_ticker is mocked even though nothing should call it — an
            # unmocked one made a live API request the moment this fixture drifted.
            with mock.patch("cli.commands.screen.screen_ticker",
                            side_effect=AssertionError("screened a ticker that was not missing")), \
                 mock.patch("cli.commands.common._fetch_sector", return_value="Tech"), \
                 mock.patch("cli.commands.common.gather_api_keys", return_value=["k"]):
                summary = rz.resume_run(run, allocate=False, log=lambda m: None, reports_root=td)
            table_written = (run / "screening_table.md").exists()
        self.assertEqual(summary["attempted"], 0)
        self.assertTrue(table_written)

    def test_a_failing_ticker_does_not_sink_the_run(self):
        with tempfile.TemporaryDirectory() as td, patch_calendar(["AAA", "BBB"]):
            meta = {**self.META, "tickers": ["AAA", "BBB"]}
            run = make_run(Path(td), "earnings_2026-08-10_1", done=["AAA"], meta=meta)
            with mock.patch("cli.commands.screen.screen_ticker", side_effect=RuntimeError("boom")), \
                 mock.patch("cli.commands.common._fetch_sector", return_value="Tech"), \
                 mock.patch("cli.commands.common.gather_api_keys", return_value=["k"]):
                summary = rz.resume_run(run, allocate=False, log=lambda m: None, reports_root=td)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["screened"], 0)

    def test_registry_records_running_then_done(self):
        from cli import jobs_registry as jr
        with tempfile.TemporaryDirectory() as td, patch_calendar(["AAA"]):
            meta = {**self.META, "tickers": ["AAA"]}
            run = make_run(Path(td), "earnings_2026-08-10_1", done=["AAA"], meta=meta)
            with mock.patch("cli.commands.common._fetch_sector", return_value="Tech"), \
                 mock.patch("cli.commands.common.gather_api_keys", return_value=["k"]):
                rz.resume_run(run, allocate=False, log=lambda m: None, reports_root=td)
            records = jr.load(td)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["type"], "resume")
        self.assertEqual(records[0]["status"], jr.DONE)

    def test_failure_marks_the_registry_and_reraises(self):
        from cli import jobs_registry as jr
        with tempfile.TemporaryDirectory() as td, patch_calendar(["AAA"]):
            meta = {**self.META, "tickers": ["AAA"]}
            run = make_run(Path(td), "earnings_2026-08-10_1", done=["AAA"], meta=meta)
            with mock.patch("cli.commands.common._fetch_sector", return_value="Tech"), \
                 mock.patch("cli.commands.common.gather_api_keys", return_value=["k"]), \
                 mock.patch("tradingagents.screening_table.write_screening_table",
                            side_effect=RuntimeError("disk full")):
                with self.assertRaises(RuntimeError):
                    rz.resume_run(run, allocate=False, log=lambda m: None, reports_root=td)
            records = jr.load(td)
        self.assertEqual(records[0]["status"], jr.ERROR)

    def test_missing_tickers_without_api_keys_fails_fast(self):
        with tempfile.TemporaryDirectory() as td, patch_calendar(["AAA", "BBB"]):
            run = make_run(Path(td), "earnings_2026-08-10_1", done=["AAA"], meta=self.META)
            with mock.patch("cli.commands.common.gather_api_keys", return_value=[]):
                with self.assertRaises(RuntimeError):
                    rz.resume_run(run, allocate=False, log=lambda m: None, reports_root=td)


if __name__ == "__main__":
    unittest.main()
