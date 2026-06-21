import unittest

import pandas as pd
import pytest

from tradingagents.allocation.pricing import compute_implied_move, format_pricing, pick_expiry


def _chain(rows):
    return pd.DataFrame(rows, columns=["strike", "bid", "ask", "lastPrice"])


@pytest.mark.unit
class ImpliedMoveTests(unittest.TestCase):
    def test_atm_straddle_math(self):
        # Spot 100: ATM strike 100, call mid 4.0, put mid 3.0 → 7% implied move
        calls = _chain([(95, 7.8, 8.2, 8.0), (100, 3.9, 4.1, 4.0), (105, 1.4, 1.6, 1.5)])
        puts = _chain([(95, 1.0, 1.2, 1.1), (100, 2.9, 3.1, 3.0), (105, 6.8, 7.2, 7.0)])
        self.assertEqual(compute_implied_move(100.0, calls, puts), 7.0)

    def test_falls_back_to_last_price_when_no_quotes(self):
        calls = _chain([(100, 0, 0, 4.0)])
        puts = _chain([(100, 0, 0, 3.0)])
        self.assertEqual(compute_implied_move(100.0, calls, puts), 7.0)

    def test_picks_nearest_strike_to_spot(self):
        calls = _chain([(90, 0, 0, 12.0), (110, 0, 0, 2.0)])
        puts = _chain([(90, 0, 0, 1.0), (110, 0, 0, 9.0)])
        # Spot 102 → ATM strike 110: (2 + 9) / 102 ≈ 10.8%
        self.assertEqual(compute_implied_move(102.0, calls, puts), 10.8)

    def test_no_common_strikes_returns_none(self):
        calls = _chain([(100, 1, 2, 1.5)])
        puts = _chain([(105, 1, 2, 1.5)])
        self.assertIsNone(compute_implied_move(100.0, calls, puts))

    def test_zero_spot_returns_none(self):
        calls = _chain([(100, 1, 2, 1.5)])
        self.assertIsNone(compute_implied_move(0, calls, calls))


@pytest.mark.unit
class PickExpiryTests(unittest.TestCase):
    EXPIRIES = ["2026-06-12", "2026-06-19", "2026-06-26"]

    def test_first_expiry_on_or_after_earnings(self):
        self.assertEqual(pick_expiry(self.EXPIRIES, "2026-06-15"), "2026-06-19")
        self.assertEqual(pick_expiry(self.EXPIRIES, "2026-06-19"), "2026-06-19")

    def test_messy_earnings_date_uses_leading_date(self):
        self.assertEqual(pick_expiry(self.EXPIRIES, "2026-06-15, 2026-06-17"), "2026-06-19")

    def test_unparseable_date_falls_back_to_first(self):
        self.assertEqual(pick_expiry(self.EXPIRIES, "Not found"), "2026-06-12")
        self.assertEqual(pick_expiry(self.EXPIRIES, None), "2026-06-12")

    def test_empty_expiries(self):
        self.assertIsNone(pick_expiry([], "2026-06-15"))


@pytest.mark.unit
class FormatPricingTests(unittest.TestCase):
    def test_full_summary(self):
        line = format_pricing({
            "price": 123.45, "market_cap": 2.5e12, "forward_pe": 31.2,
            "week52_position_pct": 88, "implied_move_pct": 6.4,
            "implied_move_expiry": "2026-06-19",
        })
        self.assertIn("$123.45", line)
        self.assertIn("$2,500.0B", line)
        self.assertIn("±6.4%", line)
        self.assertIn("2026-06-19", line)

    def test_missing_fields_degrade_to_na(self):
        line = format_pricing({"price": 50.0})
        self.assertIn("$50.00", line)
        self.assertIn("n/a", line)

    def test_none_pricing(self):
        self.assertEqual(format_pricing(None), "Not available")


if __name__ == "__main__":
    unittest.main()
