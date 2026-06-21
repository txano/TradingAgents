"""Unit tests for the payoff-asymmetry engine (#14a) — pure math, no network."""

import unittest

import pytest

from tradingagents.allocation.asymmetry import (
    assemble,
    beat_score_to_p_beat,
    compute_asymmetry,
    expected_value,
    format_asymmetry,
)


def _r(beat, move):
    return {"beat": beat, "day1_move_pct": move}


@pytest.mark.unit
class ComputeAsymmetryTests(unittest.TestCase):
    def test_conditional_means_and_fade_rate(self):
        reactions = [
            _r(True, 4.0), _r(True, -2.0), _r(True, 6.0),   # 3 beats, 1 faded (red)
            _r(False, -12.0), _r(False, -8.0),               # 2 misses
        ]
        a = compute_asymmetry(reactions)
        self.assertEqual(a["n_prints"], 5)
        self.assertEqual(a["n_beats"], 3)
        self.assertEqual(a["n_misses"], 2)
        self.assertAlmostEqual(a["e_move_beat"], (4 - 2 + 6) / 3, places=1)   # +2.7
        self.assertAlmostEqual(a["e_move_miss"], -10.0, places=1)
        self.assertAlmostEqual(a["fade_rate"], 1 / 3, places=2)               # 1 of 3 beats red

    def test_coverage_ratio_vs_implied_move(self):
        # avg |move| = (4+2+6+12+8)/5 = 6.4 ; implied 8 -> coverage 0.80
        reactions = [_r(True, 4.0), _r(True, -2.0), _r(True, 6.0),
                     _r(False, -12.0), _r(False, -8.0)]
        a = compute_asymmetry(reactions, implied_move_pct=8.0)
        self.assertAlmostEqual(a["avg_abs_move"], 6.4, places=1)
        self.assertAlmostEqual(a["coverage_ratio"], 0.80, places=2)

    def test_no_implied_move_leaves_coverage_none(self):
        a = compute_asymmetry([_r(True, 3.0)], implied_move_pct=None)
        self.assertIsNone(a["coverage_ratio"])

    def test_missing_moves_ignored(self):
        a = compute_asymmetry([_r(True, None), _r(False, None)])
        self.assertEqual(a["n_prints"], 0)
        self.assertIsNone(a["e_move_beat"])
        self.assertIsNone(a["fade_rate"])

    def test_only_beats_no_misses(self):
        a = compute_asymmetry([_r(True, 5.0), _r(True, 3.0)])
        self.assertEqual(a["n_misses"], 0)
        self.assertIsNone(a["e_move_miss"])
        self.assertEqual(a["fade_rate"], 0.0)


@pytest.mark.unit
class ProbabilityAndEVTests(unittest.TestCase):
    def test_beat_score_mapping(self):
        self.assertAlmostEqual(beat_score_to_p_beat(0), 0.50)
        self.assertAlmostEqual(beat_score_to_p_beat(5), 0.75)
        self.assertAlmostEqual(beat_score_to_p_beat(-5), 0.25)
        self.assertIsNone(beat_score_to_p_beat(None))

    def test_beat_score_clamped(self):
        self.assertEqual(beat_score_to_p_beat(20), 0.95)   # clamp high
        self.assertEqual(beat_score_to_p_beat(-20), 0.05)  # clamp low

    def test_expected_value_formula(self):
        # p=0.6, E[beat]=+4, E[miss]=-12 -> 0.6*4 + 0.4*(-12) = -2.4
        self.assertAlmostEqual(expected_value(4.0, -12.0, 0.6), -2.4, places=2)

    def test_expected_value_none_on_missing(self):
        self.assertIsNone(expected_value(None, -12.0, 0.6))
        self.assertIsNone(expected_value(4.0, -12.0, None))

    def test_negative_expectancy_despite_high_beat_prob(self):
        # The headline case: even a 70% beat model loses on −12%/+4% asymmetry.
        a = assemble(
            [{"beat": True, "day1_move_pct": 4.0}, {"beat": False, "day1_move_pct": -12.0}],
            beat_score=4, implied_move_pct=8.0,
        )
        self.assertAlmostEqual(a["p_beat"], 0.70, places=2)
        self.assertLess(a["ev"], 0)                 # negative expectancy
        self.assertLess(a["ev_to_implied"], 0.25)   # fails the long filter


@pytest.mark.unit
class AssembleAndFormatTests(unittest.TestCase):
    def test_assemble_populates_ev_fields(self):
        a = assemble([_r(True, 6.0), _r(False, -4.0)], beat_score=2, implied_move_pct=5.0)
        self.assertIn("ev", a)
        self.assertIn("p_beat", a)
        self.assertIn("ev_to_implied", a)
        self.assertAlmostEqual(a["p_beat"], 0.60, places=2)

    def test_format_full_line(self):
        a = assemble([_r(True, 6.0), _r(False, -4.0)], beat_score=2, implied_move_pct=5.0)
        line = format_asymmetry(a)
        self.assertIn("E[beat]=", line)
        self.assertIn("EV(long)=", line)
        self.assertIn("coverage=", line)

    def test_format_unavailable(self):
        self.assertEqual(format_asymmetry({}), "Not available")
        self.assertEqual(format_asymmetry({"n_prints": 0}), "Not available")


if __name__ == "__main__":
    unittest.main()
