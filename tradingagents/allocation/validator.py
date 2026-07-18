"""Deterministic constraint checks for the council's allocation output.

The synthesis LLM is asked to respect sizing rules (position cap, sector cap,
max positions, budget arithmetic) but nothing guarantees it does. This module
re-checks the parsed Allocation Score JSON against those rules so violations
can trigger a corrective re-prompt or at least be surfaced in the report.
"""

from tradingagents.allocation.regime import sizing_multiplier

SINGLE_POSITION_CAP = 0.30   # max fraction of budget per position
SECTOR_CAP = 0.35            # max fraction of budget per sector
MAX_POSITIONS = 6            # max BUY + SHORT positions

# Payoff-asymmetry gate (#14b). These are the playbook's priors — #17's
# backtest harness should recalibrate them against our own trade log.
EV_TO_IMPLIED_MIN = 0.25     # hard gate: a long needs EV/implied_move >= this
FADE_RATE_MAX = 0.60         # soft flag: share of past beats that closed red
COVERAGE_MIN = 0.70          # soft flag: avg |day-1 move| / implied move

# Implied-move sizing cap (#15a): a position's plausible one-day loss
# (amount × implied move) may not exceed this fraction of the budget. The
# playbook suggests 0.75–1.0% on a levered book; scaled down by the #15b
# regime multiplier (×0.5 risk-off, ×0.5 again on an FOMC/CPI collision).
IMPLIED_MOVE_LOSS_CAP = 0.010

_REL_TOL = 0.01              # 1% tolerance on budget arithmetic
_PCT_TOL = 0.5               # tolerance (percentage points) on pct_of_budget


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _regime_multiplier(regime: dict | None, ctx: dict | None) -> float:
    """#15b sizing multiplier: ×0.5 in risk-off, ×0.5 again on a macro collision."""
    return sizing_multiplier(regime, (ctx or {}).get("macro_collisions"))


def validate_allocation(
    alloc: dict,
    budget: int,
    contexts: list[dict],
    short_threshold: float = -5.0,
    regime: dict | None = None,
) -> list[str]:
    """Check a parsed allocation dict against the council's sizing rules.

    Args:
        alloc: Output of parse_allocation() — may be empty if parsing failed.
        budget: Total capital in dollars.
        contexts: Ticker context dicts (need ticker, sector, weighted_score).
        short_threshold: weighted_score at or below which shorts are allowed.
        regime: Optional #15b regime dict (regime.fetch_regime); risk-off halves
            the implied-move loss cap, a per-ticker macro collision halves again.

    Returns:
        Human-readable violation strings; empty list means all checks passed.
    """
    if not alloc or not isinstance(alloc.get("allocations"), list):
        return ["No parseable 'Allocation Score' JSON block found in the report."]

    violations: list[str] = []
    rows = alloc["allocations"]
    ctx_by_ticker = {c["ticker"]: c for c in contexts}

    # Ticker coverage: every screened ticker exactly once, no inventions
    seen = [str(r.get("ticker", "?")) for r in rows]
    for t in sorted({t for t in seen if seen.count(t) > 1}):
        violations.append(f"{t}: appears {seen.count(t)} times in the allocation table.")
    if ctx_by_ticker:
        missing = sorted(set(ctx_by_ticker) - set(seen))
        if missing:
            violations.append(f"Missing from allocation table: {', '.join(missing)}.")
        unknown = sorted(set(seen) - set(ctx_by_ticker))
        if unknown:
            violations.append(f"Not in the screened batch: {', '.join(unknown)}.")

    active = []
    for r in rows:
        ticker = str(r.get("ticker", "?"))
        direction = str(r.get("direction", "")).upper()
        amount = _num(r.get("amount"))

        if direction == "SKIP":
            if amount > 0:
                violations.append(f"{ticker}: marked SKIP but has a non-zero amount (${amount:,.0f}).")
            continue
        active.append((ticker, direction, amount, r))

        # Single-position cap
        cap = SINGLE_POSITION_CAP * budget
        if amount > cap * (1 + _REL_TOL):
            violations.append(
                f"{ticker}: ${amount:,.0f} exceeds the "
                f"{SINGLE_POSITION_CAP:.0%} single-position cap (${cap:,.0f})."
            )

        # pct_of_budget consistency
        pct = r.get("pct_of_budget")
        if pct is not None and budget > 0:
            actual_pct = amount / budget * 100
            if abs(_num(pct) - actual_pct) > _PCT_TOL:
                violations.append(
                    f"{ticker}: pct_of_budget {_num(pct):.1f}% does not match "
                    f"amount/budget ({actual_pct:.1f}%)."
                )

        # Short rules
        if direction == "SHORT":
            conviction = str(r.get("conviction", "")).strip().lower()
            if conviction != "high":
                violations.append(f"{ticker}: SHORT requires High conviction (got '{r.get('conviction', '?')}').")
            ctx = ctx_by_ticker.get(ticker)
            if ctx is not None:
                ws = _num(ctx.get("weighted_score"), default=None)
                if ws is not None and ws > short_threshold:
                    violations.append(
                        f"{ticker}: SHORT requires weighted_score ≤ {short_threshold:+.0f} "
                        f"(actual {ws:+.1f})."
                    )

        # Payoff-asymmetry hard gate (#14b): a long with a computable, clearly
        # negative expectancy is rejected even when the beat call is right. Only
        # fires when EV could be computed (the stock has recent misses); the
        # no-miss case is handled softly by asymmetry_advisories().
        if direction == "BUY":
            asym = (ctx_by_ticker.get(ticker, {}) or {}).get("asymmetry") or {}
            ev = asym.get("ev")
            evr = asym.get("ev_to_implied")
            if isinstance(ev, (int, float)) and (
                ev <= 0 or (isinstance(evr, (int, float)) and evr < EV_TO_IMPLIED_MIN)
            ):
                evr_str = f"{evr:+.2f}" if isinstance(evr, (int, float)) else "n/a"
                violations.append(
                    f"{ticker}: long has unfavorable payoff asymmetry (EV={ev:+.1f}%, "
                    f"EV/move={evr_str}) — non-positive expectancy into the print; "
                    f"skip or justify with a separate catalyst."
                )

        # Implied-move sizing cap (#15a/#15b): the position's plausible one-day
        # loss (amount × implied move) may not exceed a fixed fraction of the
        # budget, halved in risk-off tape and halved again on a macro collision.
        # Applies to BUYs and SHORTs alike — the move cuts both ways.
        ctx = ctx_by_ticker.get(ticker, {}) or {}
        move = ctx.get("implied_move_pct")
        if isinstance(move, (int, float)) and move > 0 and budget > 0:
            mult = _regime_multiplier(regime, ctx)
            max_amount = IMPLIED_MOVE_LOSS_CAP * mult * budget * 100 / move
            if amount > max_amount * (1 + _REL_TOL):
                mult_note = f" × {mult:g} regime multiplier" if mult < 1.0 else ""
                violations.append(
                    f"{ticker}: ${amount:,.0f} × ±{move:.1f}% implied move risks "
                    f"${amount * move / 100:,.0f} in one session — the cap is "
                    f"{IMPLIED_MOVE_LOSS_CAP:.1%} of budget{mult_note} "
                    f"(max position ≈ ${max_amount:,.0f})."
                )

    # Max positions
    if len(active) > MAX_POSITIONS:
        violations.append(
            f"{len(active)} positions allocated; the maximum is {MAX_POSITIONS} (BUY + SHORT combined)."
        )

    # Budget arithmetic
    deployed_actual = sum(a for _, _, a, _ in active)
    tol = _REL_TOL * budget
    deployed_claimed = _num(alloc.get("total_deployed"), default=None)
    if deployed_claimed is not None and abs(deployed_claimed - deployed_actual) > tol:
        violations.append(
            f"total_deployed (${deployed_claimed:,.0f}) does not match the sum of "
            f"position amounts (${deployed_actual:,.0f})."
        )
    cash = _num(alloc.get("cash_reserved"), default=None)
    if cash is not None and abs(deployed_actual + cash - budget) > tol:
        violations.append(
            f"deployed (${deployed_actual:,.0f}) + cash_reserved (${cash:,.0f}) "
            f"does not equal the budget (${budget:,.0f})."
        )
    if deployed_actual > budget * (1 + _REL_TOL):
        violations.append(
            f"Total deployed (${deployed_actual:,.0f}) exceeds the budget (${budget:,.0f})."
        )

    # Sector cap, measured against the total budget so concentrated batches
    # (1-2 positions) remain feasible. Unknown sectors can't be checked.
    if budget > 0:
        sector_totals: dict[str, float] = {}
        for ticker, _, amount, _ in active:
            sector = str(ctx_by_ticker.get(ticker, {}).get("sector", "Unknown"))
            if sector and sector != "Unknown":
                sector_totals[sector] = sector_totals.get(sector, 0.0) + amount
        for sector, total in sorted(sector_totals.items()):
            if total > SECTOR_CAP * budget * (1 + _REL_TOL):
                violations.append(
                    f"Sector '{sector}' holds ${total:,.0f} "
                    f"({total / budget:.0%} of budget) — cap is {SECTOR_CAP:.0%}."
                )

    return violations


def asymmetry_advisories(alloc: dict, contexts: list[dict]) -> list[str]:
    """Soft, non-blocking flags for longs in quality names that historically
    sell good prints.

    Fires only when EV could NOT be computed (no recent misses, so the hard
    gate in validate_allocation can't act) AND the realised history is
    unfavourable — a high fade rate or low coverage. These signal "size one
    tier smaller", not "skip" (#14b decision: soft downgrade).
    """
    if not alloc or not isinstance(alloc.get("allocations"), list):
        return []
    ctx_by_ticker = {c["ticker"]: c for c in contexts}
    advisories: list[str] = []
    for r in alloc["allocations"]:
        if str(r.get("direction", "")).upper() != "BUY" or _num(r.get("amount")) <= 0:
            continue
        ticker = str(r.get("ticker", "?"))
        asym = (ctx_by_ticker.get(ticker, {}) or {}).get("asymmetry") or {}
        if asym.get("ev") is not None:
            continue  # EV computable → the hard gate already covers this name
        fade = asym.get("fade_rate")
        cov = asym.get("coverage_ratio")
        reasons = []
        if isinstance(fade, (int, float)) and fade >= FADE_RATE_MAX:
            reasons.append(f"fade rate {fade:.0%}")
        if isinstance(cov, (int, float)) and cov <= COVERAGE_MIN:
            reasons.append(f"coverage {cov:.2f}x")
        if reasons:
            advisories.append(
                f"{ticker}: EV n/a (no recent misses) but {' and '.join(reasons)} — "
                f"historically sells good prints; size one conviction tier smaller."
            )
    return advisories


def crowding_advisories(alloc: dict, contexts: list[dict]) -> list[str]:
    """Soft, non-blocking flags for longs that are crowded into the print.

    Reads the pre-computed `crowding["flags"]` (run-up, near-52w-high, revision
    momentum). Like the asymmetry advisories these signal "size down", not skip.
    """
    if not alloc or not isinstance(alloc.get("allocations"), list):
        return []
    ctx_by_ticker = {c["ticker"]: c for c in contexts}
    advisories: list[str] = []
    for r in alloc["allocations"]:
        if str(r.get("direction", "")).upper() != "BUY" or _num(r.get("amount")) <= 0:
            continue
        ticker = str(r.get("ticker", "?"))
        flags = (ctx_by_ticker.get(ticker, {}) or {}).get("crowding", {}).get("flags") or []
        if flags:
            advisories.append(
                f"{ticker}: crowded into the print ({', '.join(flags)}) — "
                f"beat likely priced in; size one conviction tier smaller."
            )
    return advisories


def format_violations(violations: list[str]) -> str:
    """Render violations as a markdown section to append to the report."""
    lines = "\n".join(f"- {v}" for v in violations)
    return (
        "\n\n---\n### ⚠ Constraint Check\n"
        "Automated validation found rule violations the council did not resolve. "
        "Review before acting on this allocation:\n\n"
        f"{lines}\n"
    )


def format_advisories(advisories: list[str]) -> str:
    """Render the combined soft sizing advisories (asymmetry + crowding)."""
    lines = "\n".join(f"- {v}" for v in advisories)
    return (
        "\n\n---\n### ⓘ Sizing Advisories (soft — downgrade, don't skip)\n"
        "Longs whose payoff history or crowding is unfavourable for a pre-print "
        "entry. Not rule violations; treat as a size-down signal:\n\n"
        f"{lines}\n"
    )
