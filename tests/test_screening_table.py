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
    """Key rotation feeds one worker per key, so a missed key is a silent halving.

    The suffix list was hard-coded to `_8`; a .env with 16 keys contributed 8 and
    a 16-worker screen quietly ran two streams per key. The scan is unbounded now,
    which also means a test must clear the *whole* namespace, not slots 1-8 — the
    old test only cleared eight and the real environment leaked through.
    """

    PREFIX = "DEEPSEEK_API_KEY"

    def setUp(self):
        self._saved = {k: v for k, v in os.environ.items()
                       if k == self.PREFIX or k.startswith(self.PREFIX + "_")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k in [k for k in os.environ
                  if k == self.PREFIX or k.startswith(self.PREFIX + "_")]:
            os.environ.pop(k, None)
        os.environ.update(self._saved)

    def _set(self, **kw):
        for k, v in kw.items():
            os.environ[self.PREFIX + ("" if k == "base" else "_" + k[1:])] = v

    def test_collects_numbered_keys_dedup_and_blanks(self):
        from cli.commands.common import gather_api_keys
        self._set(base="a", n2="a", n3="b")          # n2 duplicates the base key
        os.environ[self.PREFIX + "_4"] = "   "        # blank → skipped
        self.assertEqual(gather_api_keys("deepseek"), ["a", "b"])
        self.assertEqual(gather_api_keys("DeepSeek"), ["a", "b"])   # case-insensitive
        self.assertEqual(gather_api_keys("nope"), [])

    def test_reads_past_the_old_eight_key_ceiling(self):
        from cli.commands.common import gather_api_keys
        self._set(base="k1", **{f"n{n}": f"k{n}" for n in range(2, 17)})
        self.assertEqual(len(gather_api_keys("deepseek")), 16)

    def test_orders_numerically_not_lexically(self):
        """_10 must follow _9, not _1 — workers map to keys by index."""
        from cli.commands.common import gather_api_keys
        self._set(base="k1", **{f"n{n}": f"k{n}" for n in range(2, 13)})
        self.assertEqual(gather_api_keys("deepseek"),
                         [f"k{n}" for n in range(1, 13)])

    def test_tolerates_gaps(self):
        from cli.commands.common import gather_api_keys
        self._set(base="a", n9="i", n16="p")          # _2.._8 absent
        self.assertEqual(gather_api_keys("deepseek"), ["a", "i", "p"])

    def test_ignores_non_numeric_suffixes(self):
        from cli.commands.common import gather_api_keys
        self._set(base="a")
        os.environ[self.PREFIX + "_BACKUP"] = "nope"
        os.environ[self.PREFIX + "_2_OLD"] = "nope"
        self.assertEqual(gather_api_keys("deepseek"), ["a"])

    def test_does_not_match_a_longer_provider_var(self):
        from cli.commands.common import gather_api_keys
        saved = os.environ.get("AZURE_DEEPSEEK_API_KEY")
        os.environ["AZURE_DEEPSEEK_API_KEY"] = "wrong"
        self._set(base="a")
        try:
            self.assertEqual(gather_api_keys("deepseek"), ["a"])
        finally:
            os.environ.pop("AZURE_DEEPSEEK_API_KEY", None)
            if saved is not None:
                os.environ["AZURE_DEEPSEEK_API_KEY"] = saved

    def test_unset_provider_returns_empty(self):
        from cli.commands.common import gather_api_keys
        self.assertEqual(gather_api_keys("deepseek"), [])


if __name__ == "__main__":
    unittest.main()
