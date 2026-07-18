import unittest

import pytest

from tradingagents.allocation.regime import (
    COLLISION_MULTIPLIER,
    RISK_OFF_MULTIPLIER,
    VIX_RISK_OFF,
    compute_regime,
    format_collisions,
    format_regime,
    macro_collisions,
    sizing_multiplier,
)


@pytest.mark.unit
class ComputeRegimeTests(unittest.TestCase):
    def test_normal_tape(self):
        r = compute_regime(spx_close=6100, spx_50dma=6000, vix=15.0)
        self.assertFalse(r["risk_off"])
        self.assertFalse(r["spx_below_50dma"])
        self.assertFalse(r["vix_elevated"])
        self.assertEqual(r["flags"], [])

    def test_spx_below_50dma_is_risk_off(self):
        r = compute_regime(spx_close=5900, spx_50dma=6000, vix=15.0)
        self.assertTrue(r["risk_off"])
        self.assertIn("SPX < 50-dma", r["flags"])

    def test_vix_above_threshold_is_risk_off(self):
        r = compute_regime(spx_close=6100, spx_50dma=6000, vix=VIX_RISK_OFF + 1)
        self.assertTrue(r["risk_off"])
        self.assertTrue(any("VIX" in f for f in r["flags"]))

    def test_both_triggers_both_flags(self):
        r = compute_regime(spx_close=5900, spx_50dma=6000, vix=30.0)
        self.assertTrue(r["risk_off"])
        self.assertEqual(len(r["flags"]), 2)

    def test_all_none_inputs_yield_unknown(self):
        r = compute_regime(None, None, None)
        self.assertIsNone(r["risk_off"])
        self.assertIsNone(r["spx_below_50dma"])
        self.assertIsNone(r["vix_elevated"])

    def test_partial_data_still_classifies(self):
        # SPX missing but VIX elevated → risk-off from the one known signal
        r = compute_regime(None, None, 25.0)
        self.assertTrue(r["risk_off"])


@pytest.mark.unit
class MacroCollisionTests(unittest.TestCase):
    def test_print_on_fomc_day_collides(self):
        hits = macro_collisions("2026-07-29")  # FOMC decision day
        self.assertTrue(any("FOMC" in h for h in hits))

    def test_print_two_sessions_before_fomc_collides(self):
        # Mon 2026-07-27 → Wed 2026-07-29 is 2 business days
        hits = macro_collisions("2026-07-27")
        self.assertTrue(any("FOMC 2026-07-29" in h for h in hits))

    def test_print_three_sessions_away_is_clear(self):
        # Fri 2026-07-24 → Wed 2026-07-29 is 3 business days
        hits = macro_collisions("2026-07-24")
        self.assertFalse(any("FOMC" in h for h in hits))

    def test_weekend_days_are_not_sessions(self):
        # CPI Tue 2026-07-14: Fri 2026-07-10 is 2 sessions away (weekend skipped)
        hits = macro_collisions("2026-07-10")
        self.assertTrue(any("CPI 2026-07-14" in h for h in hits))

    def test_unknown_date_yields_no_collisions(self):
        self.assertEqual(macro_collisions(None), [])
        self.assertEqual(macro_collisions("not-a-date"), [])


@pytest.mark.unit
class SizingMultiplierTests(unittest.TestCase):
    def test_normal_regime_full_size(self):
        self.assertEqual(sizing_multiplier({"risk_off": False}, []), 1.0)
        self.assertEqual(sizing_multiplier(None, None), 1.0)

    def test_risk_off_halves(self):
        self.assertEqual(sizing_multiplier({"risk_off": True}, []), RISK_OFF_MULTIPLIER)

    def test_collision_halves(self):
        self.assertEqual(
            sizing_multiplier({"risk_off": False}, ["FOMC 2026-07-29 (+2 sessions)"]),
            COLLISION_MULTIPLIER,
        )

    def test_risk_off_and_collision_stack(self):
        self.assertEqual(
            sizing_multiplier({"risk_off": True}, ["CPI"]),
            RISK_OFF_MULTIPLIER * COLLISION_MULTIPLIER,
        )


@pytest.mark.unit
class FormattingTests(unittest.TestCase):
    def test_format_regime_none_safe(self):
        self.assertIn("Not available", format_regime(None))
        self.assertIn("Not available", format_regime({"risk_off": None}))

    def test_format_regime_risk_off_says_halve(self):
        r = compute_regime(5900, 6000, 30.0)
        text = format_regime(r)
        self.assertIn("RISK-OFF", text)
        self.assertIn("halve", text)

    def test_format_regime_normal(self):
        r = compute_regime(6100, 6000, 15.0)
        self.assertIn("Normal", format_regime(r))

    def test_format_collisions(self):
        self.assertIn("No FOMC/CPI", format_collisions([]))
        self.assertIn("COLLISION", format_collisions(["FOMC 2026-07-29 (+0 sessions)"]))


if __name__ == "__main__":
    unittest.main()
