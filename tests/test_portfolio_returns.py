"""Portfolio return maths (TWR / MWR) in the dashboards. No network.

Both figures live in JavaScript, so these run the real functions out of the HTML
under node rather than reimplementing them — a Python copy would just be a second
thing to keep in sync, and it was exactly a silent arithmetic error that made
these worth testing:

* MWR divided Σ P&L by **Σ of every position's notional**. With the book turning
  over ~19×, $15.7M of notional on a ~$840k account reported **+0.99%** for a
  period that actually returned ~+18%.
* TWR chained one HPR **per trade**. Trades run concurrently (median 15 exits a
  day), so multiplying 1,322 of them compounded each position onto the last and
  reported **+1,580%** cumulative / **+1,515,750%** annualized.

Both now divide by a *capital base*: the largest notional exiting on any single
day. Trades that exit together were provably open together, so it is a hard
lower bound on capital at work — it can understate the return, never inflate it.
"""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import pytest

HTML = {
    "reports_site": Path("cli/static/reports_site.html"),
    "dashboard":    Path("cli/static/dashboard.html"),
}
# (capital-base fn, returns fn, cost-basis fn) per page — the two dashboards
# carry independent copies, and they must not disagree about the same trades.
ENTRY = {
    "reports_site": ("dashCapitalBase", "dashComputeReturns", "dashCostBasis"),
    "dashboard":    ("capitalBase", "computeReturns", "costBasis"),
}

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node is required to run the dashboard JS")


def _extract(page: str) -> str:
    """Pull the needed functions out of the page's biggest <script> block."""
    src = max(re.findall(r"<script[^>]*>(.*?)</script>",
                         HTML[page].read_text(encoding="utf-8"), re.S), key=len)
    wanted = ENTRY[page]
    out = []
    for name in wanted:
        m = re.search(rf"^function {name}\s*\(", src, re.M)
        if not m:
            raise AssertionError(f"{name} not found in {page}")
        # Brace-match to the end of the function.
        i = src.index("{", m.start())
        depth, j = 0, i
        while j < len(src):
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out.append(src[m.start():j + 1])
    return "\n".join(out)


def run_returns(page: str, trades: list[dict]) -> dict:
    js = _extract(page)
    with tempfile.TemporaryDirectory() as td:
        script = Path(td) / "run.js"
        script.write_text(
            js + f"\nconst t={json.dumps(trades)};"
            f"\nconsole.log(JSON.stringify({ENTRY[page][1]}(t)));",
            encoding="utf-8")
        res = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    if res.returncode:
        raise AssertionError(res.stderr)
    return json.loads(res.stdout)


def trade(ticker, price, shares, pnl, exit_date):
    return {"ticker": ticker, "entry_price": price, "shares": shares,
            "pnl": pnl, "pnl_pct": pnl / (price * shares) * 100, "exit_date": exit_date}


@pytest.mark.unit
class CapitalBaseTests(unittest.TestCase):
    def test_base_is_the_largest_same_day_exit_notional(self):
        trades = [
            trade("A", 100, 100, 500, "2026-01-05"),    # 10k
            trade("B", 100, 200, 500, "2026-01-05"),    # 20k  → 30k that day
            trade("C", 100, 250, 500, "2026-02-05"),    # 25k
        ]
        r = run_returns("reports_site", trades)
        self.assertEqual(r["base"], 30_000)
        self.assertEqual(r["peakDay"], "2026-01-05")

    def test_base_is_not_the_sum_of_notionals(self):
        """The actual bug: Σ notional counts redeployed capital many times."""
        trades = [trade(f"T{i}", 100, 100, 100, f"2026-01-{i + 1:02d}") for i in range(20)]
        r = run_returns("reports_site", trades)
        self.assertEqual(r["totalCost"], 200_000)   # Σ notional, 20 × 10k
        self.assertEqual(r["base"], 10_000)         # never more than 10k at once
        self.assertAlmostEqual(r["turnover"], 20.0)

    def test_turnover_of_one_leaves_the_two_equal(self):
        """One position, never redeployed: capital base == notional."""
        r = run_returns("reports_site", [trade("A", 100, 100, 1_000, "2026-01-05")])
        self.assertEqual(r["base"], r["totalCost"])
        self.assertAlmostEqual(r["turnover"], 1.0)


@pytest.mark.unit
class MwrTests(unittest.TestCase):
    def test_mwr_is_pnl_over_capital_at_work(self):
        trades = [trade("A", 100, 100, 1_000, "2026-01-05"),
                  trade("B", 100, 100, 1_000, "2026-03-05")]
        r = run_returns("reports_site", trades)
        # 2,000 P&L on a 10k book == +20%, NOT 2,000/20,000 == +10%
        self.assertAlmostEqual(r["mwr"], 0.20, places=6)

    def test_redeploying_capital_does_not_dilute_the_return(self):
        """Same book, same P&L, twice as many round trips → same MWR."""
        few  = [trade("A", 100, 100, 500, "2026-01-05"),
                trade("B", 100, 100, 500, "2026-02-05")]
        many = [trade("A", 100, 100, 250, "2026-01-05"),
                trade("B", 100, 100, 250, "2026-01-20"),
                trade("C", 100, 100, 250, "2026-02-05"),
                trade("D", 100, 100, 250, "2026-02-20")]
        self.assertAlmostEqual(run_returns("reports_site", few)["mwr"],
                               run_returns("reports_site", many)["mwr"], places=6)

    def test_losses_come_through_negative(self):
        r = run_returns("reports_site", [trade("A", 100, 100, -2_000, "2026-01-05")])
        self.assertAlmostEqual(r["mwr"], -0.20, places=6)


@pytest.mark.unit
class TwrTests(unittest.TestCase):
    def test_concurrent_trades_do_not_compound_on_each_other(self):
        """Ten simultaneous +10% trades are one +10% day, not 1.1**10."""
        trades = [trade(f"T{i}", 100, 100, 1_000, "2026-01-05") for i in range(10)]
        r = run_returns("reports_site", trades)
        self.assertAlmostEqual(r["twr"], 0.10, places=6)     # was 1.1**10 - 1 = +159%

    def test_sequential_days_do_compound(self):
        trades = [trade("A", 100, 100, 1_000, "2026-01-05"),
                  trade("B", 100, 100, 1_000, "2026-01-06")]
        r = run_returns("reports_site", trades)
        self.assertAlmostEqual(r["twr"], 1.10 * 1.10 - 1, places=6)

    def test_twr_stays_in_the_same_universe_as_mwr(self):
        """They differ only by intra-period compounding — never by 1000×."""
        trades = [trade(f"T{i}", 100, 100, 200, f"2026-01-{i + 1:02d}") for i in range(25)]
        r = run_returns("reports_site", trades)
        self.assertGreaterEqual(r["twr"], r["mwr"])          # compounding adds a little
        self.assertLess(r["twr"], r["mwr"] * 1.5)            # but only a little


@pytest.mark.unit
class AnnualizationTests(unittest.TestCase):
    def test_short_periods_are_not_annualized(self):
        trades = [trade("A", 100, 100, 100, "2026-01-05"),
                  trade("B", 100, 100, 100, "2026-01-07")]
        r = run_returns("reports_site", trades)
        self.assertIsNone(r["twrAnn"])
        self.assertIsNone(r["mwrAnn"])

    def test_a_total_loss_does_not_produce_a_complex_number(self):
        """(1+r)^k with r <= -1 is NaN — guard, don't render it."""
        trades = [trade("A", 100, 100, -10_000, "2026-01-05"),
                  trade("B", 100, 100, 0, "2026-06-05")]
        r = run_returns("reports_site", trades)
        for key in ("twrAnn", "mwrAnn"):
            self.assertFalse(isinstance(r[key], float) and r[key] != r[key], f"{key} is NaN")

    def test_annualization_compounds_the_period_return(self):
        trades = [trade("A", 100, 100, 1_000, "2026-01-01"),
                  trade("B", 100, 100, 0, "2026-07-01")]
        r = run_returns("reports_site", trades)
        expected = (1 + r["mwr"]) ** (365 / r["days"]) - 1
        self.assertAlmostEqual(r["mwrAnn"], expected, places=6)


@pytest.mark.unit
class BothDashboardsAgreeTests(unittest.TestCase):
    """reports_site.html and dashboard.html carry independent copies."""

    def test_same_trades_same_numbers(self):
        trades = [trade("A", 100, 100, 500, "2026-01-05"),
                  trade("B", 120, 200, -300, "2026-01-05"),
                  trade("C", 90, 300, 900, "2026-03-11")]
        a = run_returns("reports_site", trades)
        b = run_returns("dashboard", trades)
        for key in ("twr", "mwr", "base", "totalCost", "totalPnl", "days"):
            self.assertAlmostEqual(a[key], b[key], places=9, msg=key)


@pytest.mark.unit
class RegressionGuardTests(unittest.TestCase):
    """The shape of the original bug, stated as a test."""

    def test_high_turnover_book_does_not_report_a_near_zero_return(self):
        # 100 round trips of the same 10k, each +2% → +200% on the book
        trades = [trade(f"T{i}", 100, 100, 200, f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}")
                  for i in range(100)]
        r = run_returns("reports_site", trades)
        self.assertAlmostEqual(r["mwr"], 2.0, places=6)      # not 20,000/1,000,000 = +2%
        self.assertGreater(r["mwr"], 1.0)

    def test_many_concurrent_trades_do_not_report_an_absurd_twr(self):
        trades = [trade(f"T{i}", 100, 100, 50, "2026-01-05") for i in range(500)]
        r = run_returns("reports_site", trades)
        self.assertLess(r["twr"], 10.0, "per-trade HPR chaining is back")


if __name__ == "__main__":
    unittest.main()
