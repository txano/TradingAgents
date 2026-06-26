"""Unit tests for the shared screening-table writer + API-key gathering."""

import os
import tempfile
import unittest
from pathlib import Path

import pytest

from tradingagents.screening_table import render_screening_table, write_screening_table
from tradingagents.calibration.calibrator import parse_screening_table


def _row(tk, total, **kw):
    base = dict(ticker=tk, sector="Technology", earnings_date="2026-06-25",
                beat_score=1, guidance_score=1, setup_score=0, total_score=total,
                signal="BUY", confidence="High", one_liner="x")
    base.update(kw)
    return base


@pytest.mark.unit
class ScreeningTableTests(unittest.TestCase):
    def test_ranks_by_total_and_round_trips_through_parser(self):
        rows = [_row("MU", 2), _row("AAPL", 6), _row("XYZ", -3, signal="SKIP")]
        md = render_screening_table(rows, "# Earnings Screener — 2026-06-24")
        # ranked highest-first regardless of input order
        self.assertLess(md.index("AAPL"), md.index("MU"))
        self.assertLess(md.index("MU"), md.index("XYZ"))

        p = Path(tempfile.mktemp(suffix=".md"))
        p.write_text(md, encoding="utf-8")
        parsed = parse_screening_table(p)
        self.assertEqual([r["ticker"] for r in parsed], ["AAPL", "MU", "XYZ"])
        self.assertEqual(parsed[0]["total_score"], 6)
        self.assertEqual(parsed[0]["sector"], "Technology")
        self.assertEqual(parsed[2]["signal"], "SKIP")

    def test_write_creates_file(self):
        p = Path(tempfile.mktemp(suffix=".md"))
        write_screening_table([_row("MU", 2)], p, "# Header")
        self.assertTrue(p.exists())
        self.assertIn("| MU |", p.read_text())


@pytest.mark.unit
class GatherApiKeysTests(unittest.TestCase):
    def test_collects_numbered_keys_dedup_and_blanks(self):
        from cli.commands.common import gather_api_keys
        # Fully control every DEEPSEEK_API_KEY[_n] slot so the real env can't leak in.
        slots = ["DEEPSEEK_API_KEY"] + [f"DEEPSEEK_API_KEY_{n}" for n in range(2, 9)]
        saved = {k: os.environ.get(k) for k in slots}
        try:
            for k in slots:
                os.environ.pop(k, None)
            os.environ["DEEPSEEK_API_KEY"] = "a"
            os.environ["DEEPSEEK_API_KEY_2"] = "a"   # duplicate → skipped
            os.environ["DEEPSEEK_API_KEY_3"] = "b"
            self.assertEqual(gather_api_keys("deepseek"), ["a", "b"])
            self.assertEqual(gather_api_keys("DeepSeek"), ["a", "b"])  # case-insensitive
            self.assertEqual(gather_api_keys("nope"), [])
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
