import unittest

import pytest

from tradingagents.allocation.common import parse_allocation
from tradingagents.allocation.validator import (
    asymmetry_advisories,
    crowding_advisories,
    format_violations,
    validate_allocation,
)

BUDGET = 100_000

CONTEXTS = [
    {"ticker": "AAA", "sector": "Tech",       "weighted_score": 12.0},
    {"ticker": "BBB", "sector": "Tech",       "weighted_score": 6.0},
    {"ticker": "CCC", "sector": "Healthcare", "weighted_score": -8.0},
    {"ticker": "DDD", "sector": "Energy",     "weighted_score": 2.0},
]


def _alloc(rows, deployed=None, cash=None):
    amounts = sum(r["amount"] for r in rows if r["direction"] != "SKIP")
    deployed = amounts if deployed is None else deployed
    cash = BUDGET - deployed if cash is None else cash
    return {
        "total_budget": BUDGET,
        "total_deployed": deployed,
        "cash_reserved": cash,
        "allocations": rows,
    }


def _row(ticker, direction, amount, conviction="Medium"):
    return {
        "ticker": ticker,
        "direction": direction,
        "amount": amount,
        "pct_of_budget": round(amount / BUDGET * 100, 1),
        "conviction": conviction,
        "rationale": "test",
    }


@pytest.mark.unit
class ValidateAllocationTests(unittest.TestCase):
    def test_clean_allocation_passes(self):
        alloc = _alloc([
            _row("AAA", "BUY", 18_000, "High"),
            _row("BBB", "SKIP", 0),
            _row("CCC", "SHORT", 18_000, "High"),
            _row("DDD", "BUY", 18_000),
        ])
        self.assertEqual(validate_allocation(alloc, BUDGET, CONTEXTS), [])

    def test_empty_alloc_is_a_violation(self):
        violations = validate_allocation({}, BUDGET, CONTEXTS)
        self.assertEqual(len(violations), 1)
        self.assertIn("No parseable", violations[0])

    def test_single_position_cap(self):
        alloc = _alloc([
            _row("AAA", "BUY", 40_000, "High"),  # > 30% of budget
            _row("BBB", "SKIP", 0),
            _row("CCC", "SKIP", 0),
            _row("DDD", "SKIP", 0),
        ])
        violations = validate_allocation(alloc, BUDGET, CONTEXTS)
        self.assertTrue(any("single-position cap" in v for v in violations))

    def test_sector_cap(self):
        # Tech = 50k of 60k deployed = 83% > 35%
        alloc = _alloc([
            _row("AAA", "BUY", 28_000, "High"),
            _row("BBB", "BUY", 22_000),
            _row("CCC", "SHORT", 10_000, "High"),
            _row("DDD", "SKIP", 0),
        ])
        violations = validate_allocation(alloc, BUDGET, CONTEXTS)
        self.assertTrue(any("Sector 'Tech'" in v for v in violations))

    def test_max_positions(self):
        contexts = [
            {"ticker": f"T{i}", "sector": "Unknown", "weighted_score": 8.0}
            for i in range(7)
        ]
        alloc = _alloc([_row(f"T{i}", "BUY", 10_000) for i in range(7)])
        violations = validate_allocation(alloc, BUDGET, contexts)
        self.assertTrue(any("maximum is 6" in v for v in violations))

    def test_short_requires_high_conviction(self):
        alloc = _alloc([
            _row("AAA", "SKIP", 0),
            _row("BBB", "SKIP", 0),
            _row("CCC", "SHORT", 10_000, "Medium"),
            _row("DDD", "SKIP", 0),
        ])
        violations = validate_allocation(alloc, BUDGET, CONTEXTS)
        self.assertTrue(any("SHORT requires High conviction" in v for v in violations))

    def test_short_requires_low_weighted_score(self):
        alloc = _alloc([
            _row("AAA", "SKIP", 0),
            _row("BBB", "SHORT", 10_000, "High"),  # weighted_score +6.0
            _row("CCC", "SKIP", 0),
            _row("DDD", "SKIP", 0),
        ])
        violations = validate_allocation(alloc, BUDGET, CONTEXTS, short_threshold=-5)
        self.assertTrue(any("weighted_score" in v for v in violations))

    def test_budget_arithmetic_mismatch(self):
        alloc = _alloc(
            [
                _row("AAA", "BUY", 20_000, "High"),
                _row("BBB", "SKIP", 0),
                _row("CCC", "SKIP", 0),
                _row("DDD", "SKIP", 0),
            ],
            deployed=50_000,  # claims more than the 20k actually allocated
        )
        violations = validate_allocation(alloc, BUDGET, CONTEXTS)
        self.assertTrue(any("total_deployed" in v for v in violations))

    def test_cash_plus_deployed_must_equal_budget(self):
        alloc = _alloc(
            [
                _row("AAA", "BUY", 20_000, "High"),
                _row("BBB", "SKIP", 0),
                _row("CCC", "SKIP", 0),
                _row("DDD", "SKIP", 0),
            ],
            cash=10_000,  # 20k + 10k != 100k
        )
        violations = validate_allocation(alloc, BUDGET, CONTEXTS)
        self.assertTrue(any("cash_reserved" in v for v in violations))

    def test_skip_with_nonzero_amount(self):
        alloc = _alloc([
            _row("AAA", "SKIP", 5_000),
            _row("BBB", "SKIP", 0),
            _row("CCC", "SKIP", 0),
            _row("DDD", "SKIP", 0),
        ], deployed=0, cash=BUDGET)
        violations = validate_allocation(alloc, BUDGET, CONTEXTS)
        self.assertTrue(any("marked SKIP" in v for v in violations))

    def test_missing_and_unknown_tickers(self):
        alloc = _alloc([
            _row("AAA", "BUY", 10_000),
            _row("ZZZ", "BUY", 10_000),  # not screened
        ])
        violations = validate_allocation(alloc, BUDGET, CONTEXTS)
        self.assertTrue(any("Missing from allocation table" in v for v in violations))
        self.assertTrue(any("Not in the screened batch" in v for v in violations))

    def test_pct_of_budget_mismatch(self):
        row = _row("AAA", "BUY", 10_000)
        row["pct_of_budget"] = 25.0  # actual is 10%
        alloc = _alloc([row, _row("BBB", "SKIP", 0), _row("CCC", "SKIP", 0), _row("DDD", "SKIP", 0)])
        violations = validate_allocation(alloc, BUDGET, CONTEXTS)
        self.assertTrue(any("pct_of_budget" in v for v in violations))

    def test_format_violations_renders_section(self):
        section = format_violations(["AAA: too big.", "Sector 'Tech' over cap."])
        self.assertIn("### ⚠ Constraint Check", section)
        self.assertIn("- AAA: too big.", section)


@pytest.mark.unit
class AsymmetryGateTests(unittest.TestCase):
    """#14b — hard EV gate (violation) vs soft fade/coverage advisory."""

    def _ctx(self, asym_by_ticker):
        out = []
        for c in CONTEXTS:
            cc = dict(c)
            if c["ticker"] in asym_by_ticker:
                cc["asymmetry"] = asym_by_ticker[c["ticker"]]
            out.append(cc)
        return out

    def test_negative_ev_long_is_hard_violation(self):
        ctx = self._ctx({"AAA": {"ev": -0.8, "ev_to_implied": -0.10}})
        alloc = _alloc([_row("AAA", "BUY", 18_000, "High"),
                        _row("BBB", "SKIP", 0), _row("CCC", "SKIP", 0), _row("DDD", "SKIP", 0)])
        v = validate_allocation(alloc, BUDGET, ctx)
        self.assertTrue(any("unfavorable payoff asymmetry" in x for x in v))

    def test_low_ev_ratio_long_is_hard_violation(self):
        # EV positive but EV/move below 0.25 -> still rejected
        ctx = self._ctx({"AAA": {"ev": 0.5, "ev_to_implied": 0.10}})
        alloc = _alloc([_row("AAA", "BUY", 18_000, "High"),
                        _row("BBB", "SKIP", 0), _row("CCC", "SKIP", 0), _row("DDD", "SKIP", 0)])
        v = validate_allocation(alloc, BUDGET, ctx)
        self.assertTrue(any("EV/move" in x for x in v))

    def test_healthy_ev_long_passes(self):
        ctx = self._ctx({"AAA": {"ev": 3.0, "ev_to_implied": 0.50}})
        alloc = _alloc([_row("AAA", "BUY", 18_000, "High"),
                        _row("BBB", "SKIP", 0), _row("CCC", "SKIP", 0), _row("DDD", "SKIP", 0)])
        self.assertEqual(validate_allocation(alloc, BUDGET, ctx), [])

    def test_negative_ev_short_not_gated(self):
        # Negative EV-of-long is fine for a SHORT; gate must not fire.
        ctx = self._ctx({"CCC": {"ev": -5.0, "ev_to_implied": -0.6}})
        alloc = _alloc([_row("CCC", "SHORT", 10_000, "High"),
                        _row("AAA", "SKIP", 0), _row("BBB", "SKIP", 0), _row("DDD", "SKIP", 0)])
        v = validate_allocation(alloc, BUDGET, ctx)
        self.assertFalse(any("payoff asymmetry" in x for x in v))

    def test_missing_asymmetry_no_gate(self):
        # Backward compat: contexts without asymmetry never trip the gate.
        alloc = _alloc([_row("AAA", "BUY", 18_000, "High"),
                        _row("BBB", "SKIP", 0), _row("CCC", "SKIP", 0), _row("DDD", "SKIP", 0)])
        self.assertEqual(validate_allocation(alloc, BUDGET, CONTEXTS), [])

    def test_soft_advisory_on_no_miss_high_fade(self):
        ctx = self._ctx({"AAA": {"ev": None, "fade_rate": 0.62, "coverage_ratio": 0.63}})
        alloc = _alloc([_row("AAA", "BUY", 18_000, "High"),
                        _row("BBB", "SKIP", 0), _row("CCC", "SKIP", 0), _row("DDD", "SKIP", 0)])
        # Not a hard violation...
        self.assertEqual(validate_allocation(alloc, BUDGET, ctx), [])
        # ...but a soft advisory fires.
        adv = asymmetry_advisories(alloc, ctx)
        self.assertEqual(len(adv), 1)
        self.assertIn("AAA", adv[0])
        self.assertIn("fade rate", adv[0])

    def test_no_advisory_when_ev_computable(self):
        # EV computable -> hard gate's domain, not the soft advisory's.
        ctx = self._ctx({"AAA": {"ev": -1.0, "ev_to_implied": -0.2, "fade_rate": 0.9}})
        alloc = _alloc([_row("AAA", "BUY", 18_000, "High"),
                        _row("BBB", "SKIP", 0), _row("CCC", "SKIP", 0), _row("DDD", "SKIP", 0)])
        self.assertEqual(asymmetry_advisories(alloc, ctx), [])

    def test_no_advisory_for_healthy_history(self):
        ctx = self._ctx({"AAA": {"ev": None, "fade_rate": 0.2, "coverage_ratio": 1.3}})
        alloc = _alloc([_row("AAA", "BUY", 18_000, "High"),
                        _row("BBB", "SKIP", 0), _row("CCC", "SKIP", 0), _row("DDD", "SKIP", 0)])
        self.assertEqual(asymmetry_advisories(alloc, ctx), [])


@pytest.mark.unit
class CrowdingAdvisoryTests(unittest.TestCase):
    """#14c — crowded longs produce a soft advisory, never a hard violation."""

    def _ctx(self, crowding_by_ticker):
        out = []
        for c in CONTEXTS:
            cc = dict(c)
            if c["ticker"] in crowding_by_ticker:
                cc["crowding"] = crowding_by_ticker[c["ticker"]]
            out.append(cc)
        return out

    def test_crowded_long_gets_advisory(self):
        ctx = self._ctx({"AAA": {"flags": ["1m run-up +18%", "within 2% of 52w high"]}})
        alloc = _alloc([_row("AAA", "BUY", 18_000, "High"),
                        _row("BBB", "SKIP", 0), _row("CCC", "SKIP", 0), _row("DDD", "SKIP", 0)])
        self.assertEqual(validate_allocation(alloc, BUDGET, ctx), [])  # not a hard violation
        adv = crowding_advisories(alloc, ctx)
        self.assertEqual(len(adv), 1)
        self.assertIn("crowded", adv[0])
        self.assertIn("run-up", adv[0])

    def test_no_flags_no_advisory(self):
        ctx = self._ctx({"AAA": {"flags": []}})
        alloc = _alloc([_row("AAA", "BUY", 18_000, "High"),
                        _row("BBB", "SKIP", 0), _row("CCC", "SKIP", 0), _row("DDD", "SKIP", 0)])
        self.assertEqual(crowding_advisories(alloc, ctx), [])

    def test_crowded_short_not_advised(self):
        ctx = self._ctx({"CCC": {"flags": ["1m run-up +18%"]}})
        alloc = _alloc([_row("CCC", "SHORT", 10_000, "High"),
                        _row("AAA", "SKIP", 0), _row("BBB", "SKIP", 0), _row("DDD", "SKIP", 0)])
        self.assertEqual(crowding_advisories(alloc, ctx), [])

    def test_missing_crowding_safe(self):
        alloc = _alloc([_row("AAA", "BUY", 18_000, "High"),
                        _row("BBB", "SKIP", 0), _row("CCC", "SKIP", 0), _row("DDD", "SKIP", 0)])
        self.assertEqual(crowding_advisories(alloc, CONTEXTS), [])


@pytest.mark.unit
class ParseAllocationTests(unittest.TestCase):
    def test_prefers_anchored_block(self):
        report = (
            "intro\n```json\n{\"stray\": true}\n```\n"
            "### Allocation Score\n```json\n{\"total_budget\": 1}\n```\n"
        )
        self.assertEqual(parse_allocation(report), {"total_budget": 1})

    def test_falls_back_to_last_block(self):
        report = (
            "```json\n{\"stray\": true}\n```\n"
            "text\n```json\n{\"total_budget\": 2}\n```\n"
        )
        self.assertEqual(parse_allocation(report), {"total_budget": 2})

    def test_no_block_returns_empty(self):
        self.assertEqual(parse_allocation("no json here"), {})


if __name__ == "__main__":
    unittest.main()
