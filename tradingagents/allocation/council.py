"""AI Council for allocation decisions.

Pipeline:
  1. Five advisors (different analytical styles) review all tickers in parallel.
  2. Each advisor's response is anonymized (Alpha … Epsilon).
  3. All five advisors cross-review the anonymized batch in parallel, answering:
       - Which perspective is strongest?
       - Which has the biggest blind spot?
       - What did all five miss?
  4. A synthesis pass reads the original data + all perspectives + all reviews
     and produces the final allocation report.
"""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from tradingagents.allocation.common import parse_allocation
from tradingagents.allocation.regime import VIX_RISK_OFF, format_regime, sizing_multiplier
from tradingagents.allocation.validator import (
    IMPLIED_MOVE_LOSS_CAP,
    asymmetry_advisories,
    crowding_advisories,
    format_advisories,
    format_violations,
    validate_allocation,
)
from tradingagents.allocation.weights import DEFAULTS as _WEIGHT_DEFAULTS

# ── Advisor personas ───────────────────────────────────────────────────────────

_ADVISORS = [
    {
        "label": "Alpha",
        "name":  "Contrarian",
        "system": (
            "You are a seasoned contrarian analyst at a hedge fund. "
            "Your job is to stress-test every bull case and find what can go wrong. "
            "You are skeptical of consensus, you surface hidden risks, and you ask "
            "whether the beat or guidance surprise is already priced in. "
            "You are not a perma-bear — give credit where it's due — but your primary "
            "value is identifying failure modes and tail risks that optimists miss."
        ),
    },
    {
        "label": "Beta",
        "name":  "First Principles",
        "system": (
            "You are a first-principles thinker. You strip away narrative and "
            "rebuild each investment thesis from scratch using only fundamental facts: "
            "what does this company actually do, what does 'beating estimates' mean for "
            "their unit economics, is the edge structural or temporary, and does the "
            "setup make sense independent of what other analysts think? "
            "Question assumptions others take for granted."
        ),
    },
    {
        "label": "Gamma",
        "name":  "Expansionist",
        "system": (
            "You are an expansionist analyst who finds upside that others miss. "
            "You look for underappreciated catalysts, optionality not priced in, "
            "second-order effects of guidance beats, and reasons why the market may "
            "be significantly underestimating these opportunities. "
            "You are constructive by nature and seek asymmetric upside."
        ),
    },
    {
        "label": "Delta",
        "name":  "Outsider",
        "system": (
            "You are an intelligent outsider with no industry-specific knowledge or "
            "preconceptions about these companies. You reason purely from numbers, "
            "logical consistency, and pattern recognition. You evaluate whether the "
            "data coheres, whether risk/reward is rational, and whether the setup "
            "makes sense from a purely objective, quantitative perspective. "
            "You have no emotional attachment to any narrative."
        ),
    },
    {
        "label": "Epsilon",
        "name":  "Executor",
        "system": (
            "You are a pragmatic executor who only cares about one thing: making money. "
            "Cut through all the analysis and give blunt verdicts. Which trades will "
            "make money, which won't, and how would you size them if it were your own "
            "capital? You have no patience for hedge-everything analysis. "
            "Be direct, decisive, and specific about what you'd actually do."
        ),
    },
]

# ── Prompt templates ───────────────────────────────────────────────────────────

_ADVISOR_HUMAN = """\
Budget: ${budget:,}
Analysis Date: {trade_date}

=== TICKER ANALYSES ===
{ticker_sections}
---

For each ticker give ONE line using this exact format:
TICKER | BUY/SHORT/SKIP | High/Medium/Low | your key reason (max 130 chars)

Then write 3-4 sentences giving your overall portfolio view: which positions \
you are most and least confident in, cross-portfolio risks you see, and whether \
the batch as a whole is worth deploying capital into.

Be direct and specific. Do not repeat the input data.
"""

_REVIEW_TASK = (
    "You are now acting as a meta-reviewer. Several advisors (including you) "
    "independently reviewed the same tickers; their perspectives are anonymized "
    "below and one of them may be your own. Evaluate every perspective purely on "
    "the quality of its reasoning, through the lens of your own analytical style."
)

_REVIEW_HUMAN = """\
{n} advisors independently reviewed the same set of pre-earnings tickers. \
Their perspectives are anonymized below.

=== ADVISOR PERSPECTIVES ===
{perspectives}

---
Answer each question directly and specifically (reference tickers or arguments where relevant):

**1. Strongest perspective**
Which advisor made the strongest overall case and why? (2 sentences)

**2. Biggest blind spot**
Which advisor had the most significant blind spot or missed risk, and what was it? \
(2 sentences)

**3. Collective gap**
What did ALL the advisors miss or underweight — something none of them addressed \
adequately? (2-3 sentences)
"""

_SYNTHESIS_SYSTEM = """\
You are a senior portfolio manager making final capital allocation decisions. \
You have five independent analytical perspectives on a set of pre-earnings trades, \
plus a meta-review from each advisor evaluating all five, a set of historical \
lessons from past trades, a fundamentals quality score for each company, and \
historical screening data showing how each ticker has scored in past runs. \
Your job is to synthesize all of this into a rigorous, actionable allocation.

Core rules:
- Total budget: ${budget:,}
- Single position cap: 30% of budget (${single_cap:,})
- No sector may exceed 35% of the total budget
- Maximum 6 positions total (BUY + SHORT combined). Rank all candidates by \
overall conviction; allocate only to the top 6. Leave the rest as SKIP.
- SHORTS require High conviction only AND weighted_score ≤ {short_thresh:+d}
- Sizing tiers (% of budget):
    High conviction  → 15–25%
    Medium conviction → 7–14%
    Low conviction   → ≤ 6% or SKIP
- Weights used: beat={beat_w:.2f} × beat_score + guidance={guidance_w:.2f} × \
guidance_score + setup={setup_w:.2f} × setup_score + fundamentals={fundamentals_w:.2f} × fundamentals_score
  Fundamentals carries the highest weight — business quality is the primary lens. \
Beat expectation carries the lowest weight; a single quarter's beat is easily noisy.
  Weights are configured priorities (adjustable via `tradingagents weights`), \
not calibrated predictive coefficients.
- Score interpretation (thresholds scale with the configured weights; max \
possible weighted_score = {max_score:+d}):
    weighted_score ≥ {strong_thresh:+d}  → strong long signal
    {moderate_thresh:+d} to {strong_minus:+d}            → moderate long signal
    ≤ {weak_thresh:+d}                 → weak; skip unless compelling brief and strong fundamentals
    ≤ {short_thresh:+d}                 → short candidate (High conviction only)
- Fundamentals are already factored into weighted_score above. \
They also act as a sizing guardrail within the allocated conviction tier:
    +3 to +5 → quality business; full sizing within conviction tier permitted
     0 to +2 → adequate; Medium conviction is fine; exercise discretion at High
    -1 to -5 → deteriorating; reduce sizing by one tier; -3 or worse rarely merits capital
- Historical screening trend: consistent strong signals across multiple screenings \
increase confidence; a Deteriorating trend is a risk flag even if the current score is positive.
- Implied earnings move (when available) is the market's priced-in expectation \
for the event. When the implied move is large relative to your conviction, size \
down — the event is expensive to bet on. When a brief's thesis merely restates \
what the implied move already prices in, treat the edge as weak and prefer SKIP.
- Asymmetry/EV (from the stock's own last several prints) is decisive for longs. \
EV(long) is the probability-weighted expected day-1 move; EV/move is EV divided by \
the implied move. Two rules, applied to BUY candidates only:
    • HARD: do NOT open a long when EV ≤ 0 or EV/move < +0.25 — the payoff is \
structurally poor even if the beat call is right (high-multiple names routinely \
show E[miss] far larger than E[beat]). Such longs are rejected by validation; SKIP them.
    • SOFT: when EV is "n/a" (the stock has no recent misses, so EV can't be \
computed) but the fade rate is ≥ 60% or coverage ≤ 0.70, the stock historically \
sells good prints — cut conviction by one tier rather than skipping.
  (Coverage > 1 means it habitually moves more than priced in — owning the event \
is justified; coverage < 1 favours selling premium over owning stock.)
- Crowding (run-up into the print) signals a beat is already priced in — the \
classic "beat-and-fall" trap. When the Crowding line shows FLAGS (a large 1m \
run-up, strong outperformance vs the sector, sitting within a few % of the 52-week \
high, or a cluster of upward estimate revisions), the bar to clear is higher than \
the headline number: cut conviction one tier on that long. Crowding is a soft \
size-down signal, not an automatic SKIP.
- Peers (read-through from industry peers that already reported this season) is a \
soft sector gate. A "⚠ elevated bar" tag means at least one peer BEAT AND STILL \
FELL — the most dangerous pattern (good results, bad reaction): the sector is \
punishing prints regardless of quality, so cut conviction one tier on longs in that \
name. Peers shown missing (M) on a shared driver is a weaker tailwind — lean toward \
SKIP over BUY. Peers beating and holding their gains is a genuine tailwind.
- Implied-move loss cap (HARD, #15a): a position's plausible one-day loss is \
amount × implied move. Keep every position's amount × implied_move at or below \
{im_cap_pct:.2f}% of budget (${im_cap:,} at the current regime multiplier) — so a \
±10% implied-move name gets HALF the dollars of a ±5% name at equal conviction. \
Size off the implied move, not conviction alone; positions above this cap are \
rejected by validation. When the implied move is unknown, size conservatively as \
if it were large.
- Tactical regime gates (#15b): the MARKET REGIME block reports risk-off status \
(SPX below its 50-dma or VIX > {vix_risk_off:.0f}). In risk-off tape beats get \
sold — halve ALL position sizes (the loss cap above already reflects this via \
the regime multiplier). A ticker whose Macro line shows a COLLISION (FOMC or CPI \
within ±2 sessions of the print) will have a contaminated reaction — halve that \
position again, or prefer SKIP.
- Insider signal (the Insider line): a "cluster buy" (several distinct insiders \
buying) or a "reversal" (an insider who was selling and starts buying) is a genuine \
bullish confirmation — historically strong forward returns — and can support a \
higher conviction tier, especially alongside a cheap valuation. A notable single \
buy is a milder positive. Discount routine "net selling"/"heavy net selling" — \
insiders sell for many non-signal reasons (diversification, 10b5-1 plans), so do \
NOT treat selling as bearish on its own. Insider buying supports a long; it does \
not by itself override an unfavorable EV/asymmetry or a beat-and-fall setup.
- Historical lessons from past trades are provided — apply them when relevant; \
they represent real patterns observed in this specific strategy.
- Cash not deployed is acceptable; never force marginal trades.
"""

_SYNTHESIS_HUMAN = """\
Budget: ${budget:,}
Analysis Date: {trade_date}

=== MARKET REGIME ===
{regime_block}

=== TICKER DATA ===
{ticker_sections}

=== FIVE ADVISOR PERSPECTIVES ===
{perspectives}

=== CROSS-REVIEWS ===
{reviews}

=== HISTORICAL LESSONS FROM PAST TRADES ===
{lessons_block}

---
Produce the final Portfolio Allocation Report:

---
## Portfolio Allocation — {trade_date}
**Total Budget:** ${budget:,}

---
### Council Summary

[3-4 sentences: what the council agreed on, key tensions or disagreements between advisors, and how you resolved them.]

---
### Rationale

[4-6 sentences: which signals were strong enough to act on, what you passed on, how you weighted advisor perspectives, how you managed sector and long/short balance.]

---
### Allocations

| # | Ticker | Direction | Amount ($) | % Budget | Conviction | Earnings | Rationale |
|---|--------|-----------|------------|----------|------------|----------|-----------|
[one row per ticker — include every ticker, even those with $0 / SKIP]

---
### Summary

- **Total Deployed:** $<sum of all non-zero allocations>
- **Cash Reserved:** $<budget minus deployed>
- **Long Exposure:** $<sum of BUY allocations>
- **Short Exposure:** $<sum of SHORT allocations>

---
### Allocation Score
```json
{{
  "total_budget": {budget},
  "total_deployed": <integer>,
  "cash_reserved": <integer>,
  "long_exposure": <integer>,
  "short_exposure": <integer>,
  "allocations": [
    {{
      "ticker": "<ticker>",
      "direction": "<BUY|SHORT|SKIP>",
      "amount": <integer, 0 for SKIP>,
      "pct_of_budget": <float, one decimal>,
      "conviction": "<High|Medium|Low>",
      "rationale": "<one sentence max 130 chars>"
    }}
  ]
}}
```
---
"""

_CORRECTION_HUMAN = """\
Your previous Portfolio Allocation Report failed automated constraint validation.

=== PREVIOUS REPORT ===
{report}

=== RULE VIOLATIONS ===
{violations}

---
Regenerate the COMPLETE report in the exact same format (including the \
### Allocation Score JSON block), changing only what is needed to fix every \
violation listed above. Keep all decisions that were not flagged.
"""

# ── Helpers ───────────────────────────────────────────────────────────────────

_TICKER_SECTION = """\
### {ticker}
Sector: {sector} | Earnings: {earnings_date}
Scores (raw):     beat={beat_score:+d}  guidance={guidance_score:+d}  setup={setup_score:+d}  total={total_score:+d}
Scores (weighted, w_beat={beat_weight:.2f} w_guidance={guidance_weight:.2f} w_setup={setup_weight:.2f} w_fund={fundamentals_weight:.2f}):  weighted={weighted_score:+.1f}
Fundamentals:     score={fundamentals_score:+d}/5 | B/S: {bs_quality} | Margins: {margin_trend} | Growth: {growth_quality}
Fundamentals note: {fundamentals_summary}
Pricing:          {pricing_summary}
Asymmetry/EV:     {asymmetry_summary}
Crowding:         {crowding_summary}
Peers:            {peer_summary}
Insider:          {insider_summary}
Macro:            {macro_summary}
History ({historical_count} prior screening(s), avg_total={historical_avg_total}, trend={score_trend}):
  {historical_brief}
Signal: {signal} | Confidence: {confidence}
One-liner: {one_liner}

Portfolio Manager Decision:
{pm_decision}

Earnings Brief:
{brief_summary}
"""


def _format_sections(contexts: list[dict]) -> str:
    parts = []
    for ctx in contexts:
        avg = ctx.get("historical_avg_total")
        avg_str = f"{avg:+.1f}" if avg is not None else "n/a"
        parts.append(
            _TICKER_SECTION.format(
                ticker=ctx["ticker"],
                sector=ctx.get("sector", "Unknown"),
                earnings_date=ctx.get("earnings_date", "unknown"),
                beat_score=ctx.get("beat_score", 0),
                guidance_score=ctx.get("guidance_score", 0),
                setup_score=ctx.get("setup_score", 0),
                total_score=ctx.get("total_score", 0),
                weighted_score=ctx.get("weighted_score", ctx.get("total_score", 0)),
                beat_weight=ctx.get("beat_weight", _WEIGHT_DEFAULTS["beat"]),
                guidance_weight=ctx.get("guidance_weight", _WEIGHT_DEFAULTS["guidance"]),
                setup_weight=ctx.get("setup_weight", _WEIGHT_DEFAULTS["setup"]),
                fundamentals_weight=ctx.get("fundamentals_weight", _WEIGHT_DEFAULTS["fundamentals"]),
                fundamentals_score=ctx.get("fundamentals_score", 0),
                bs_quality=ctx.get("bs_quality", "Adequate"),
                margin_trend=ctx.get("margin_trend", "Stable"),
                growth_quality=ctx.get("growth_quality", "Medium"),
                fundamentals_summary=ctx.get("fundamentals_summary", ""),
                pricing_summary=ctx.get("pricing_summary", "Not available"),
                asymmetry_summary=ctx.get("asymmetry_summary", "Not available"),
                crowding_summary=ctx.get("crowding_summary", "Not available"),
                peer_summary=ctx.get("peer_summary", "Not available"),
                insider_summary=ctx.get("insider_summary", "Not available"),
                macro_summary=ctx.get("macro_summary", "Not available"),
                historical_count=ctx.get("historical_count", 0),
                historical_avg_total=avg_str,
                score_trend=ctx.get("score_trend", "New ticker"),
                historical_brief=ctx.get("historical_brief", "No prior screenings found."),
                signal=ctx.get("signal", "?"),
                confidence=ctx.get("confidence", "?"),
                one_liner=ctx.get("one_liner", ""),
                pm_decision=ctx.get("pm_decision", "Not available"),
                brief_summary=ctx.get("brief_summary", "Not available"),
            )
        )
    return "\n".join(parts)


def _call(llm, system: str, human: str) -> str:
    """Single LLM call, returns content string."""
    return llm.invoke([("system", system), ("human", human)]).content


# ── Council runner ─────────────────────────────────────────────────────────────

def run_council(
    llm,
    ticker_contexts: list[dict],
    budget: int,
    trade_date: str,
    weights: dict,
    lessons_block: str = "",
    progress_cb=None,
    advisor_llms: list | None = None,
    regime: dict | None = None,
) -> str:
    """Run the full council pipeline and return the final allocation report.

    Args:
        llm: LangChain-compatible chat model (always used for the synthesis).
        ticker_contexts: Enriched list of dicts from AllocationLayer._build_contexts().
        budget: Total capital in dollars.
        trade_date: YYYY-MM-DD string.
        weights: Dict with beat/guidance/setup/fundamentals floats from weights.py.
        progress_cb: Optional callable(message: str) for progress updates.
        advisor_llms: Optional list of chat models assigned to advisors
            round-robin (perspective + review), so the council's views come
            from genuinely different models. None/empty = all advisors use llm.
        regime: Optional #15b market-regime dict (regime.fetch_regime). Shown to
            the synthesis LLM and used to scale the implied-move loss cap.
    """
    beat_w         = weights.get("beat",         _WEIGHT_DEFAULTS["beat"])
    guidance_w     = weights.get("guidance",     _WEIGHT_DEFAULTS["guidance"])
    setup_w        = weights.get("setup",        _WEIGHT_DEFAULTS["setup"])
    fundamentals_w = weights.get("fundamentals", _WEIGHT_DEFAULTS["fundamentals"])

    # Signal thresholds scale with the configured weights (each raw score is
    # -5..+5, so the max possible weighted_score is 5 × sum of weights). With
    # default weights this yields the historical +11 / +5 / -5 cutoffs.
    max_score = int(5 * (beat_w + guidance_w + setup_w + fundamentals_w) + 0.5)
    strong_thresh = int(0.5 * max_score + 0.5)
    moderate_thresh = int(0.25 * max_score + 0.5)
    short_thresh = -moderate_thresh

    ticker_sections = _format_sections(ticker_contexts)

    def _log(msg: str) -> None:
        if progress_cb:
            progress_cb(msg)

    # ── Step 1: five advisors in parallel ──────────────────────────────────────
    _log("Consulting advisors (5 parallel)...")
    advisor_responses: dict[str, str] = {}

    llm_by_label = {
        a["label"]: (advisor_llms[i % len(advisor_llms)] if advisor_llms else llm)
        for i, a in enumerate(_ADVISORS)
    }

    def _run_advisor(advisor: dict) -> tuple[str, str]:
        human = _ADVISOR_HUMAN.format(
            budget=budget,
            trade_date=trade_date,
            ticker_sections=ticker_sections,
        )
        return advisor["label"], _call(llm_by_label[advisor["label"]], advisor["system"], human)

    names_by_label = {a["label"]: a["name"] for a in _ADVISORS}

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_run_advisor, a): a["label"] for a in _ADVISORS}
        for fut in as_completed(futures):
            label = futures[fut]
            try:
                lbl, resp = fut.result()
                advisor_responses[lbl] = resp
                _log(f"  Advisor {lbl} ({names_by_label[lbl]}) done.")
            except Exception as exc:
                _log(f"  Advisor {label} failed: {exc}")

    # Failed advisors are dropped entirely so their error text never reaches
    # the cross-review or synthesis prompts.
    active_advisors = [a for a in _ADVISORS if a["label"] in advisor_responses]
    active_labels = [a["label"] for a in active_advisors]
    if len(active_labels) < 2:
        raise RuntimeError(
            f"Council aborted: only {len(active_labels)} of {len(_ADVISORS)} advisors responded."
        )

    # ── Step 2: format anonymized perspectives ─────────────────────────────────
    _log(f"Running cross-reviews ({len(active_advisors)} parallel)...")
    note = (
        f"[Note: {len(active_labels)} of {len(_ADVISORS)} advisors responded; "
        "the rest failed and are omitted.]\n\n"
        if len(active_labels) < len(_ADVISORS)
        else ""
    )
    perspectives_block = note + "\n\n".join(
        f"--- Advisor {lbl} ---\n{advisor_responses[lbl]}" for lbl in active_labels
    )

    # ── Step 3: cross-reviews in parallel, each through its advisor's persona ──
    review_responses: dict[str, str] = {}

    def _run_review(advisor: dict) -> tuple[str, str]:
        system = advisor["system"] + "\n\n" + _REVIEW_TASK
        human = _REVIEW_HUMAN.format(
            n=len(active_labels), perspectives=perspectives_block
        )
        return advisor["label"], _call(llm_by_label[advisor["label"]], system, human)

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_run_review, a): a["label"] for a in active_advisors}
        for fut in as_completed(futures):
            label = futures[fut]
            try:
                lbl, resp = fut.result()
                review_responses[lbl] = resp
                _log(f"  Review from {lbl} done.")
            except Exception as exc:
                _log(f"  Review from {label} failed: {exc}")

    # ── Step 4: synthesis ──────────────────────────────────────────────────────
    _log("Synthesizing final allocation...")
    reviews_block = "\n\n".join(
        f"--- {lbl}'s review ---\n{review_responses[lbl]}"
        for lbl in active_labels
        if lbl in review_responses
    ) or "[No cross-reviews available.]"

    # #15: the loss cap shown to the LLM reflects the global risk-off multiplier;
    # per-ticker macro collisions halve it again (stated as a rule, enforced by
    # the validator via each context's macro_collisions).
    regime_mult = sizing_multiplier(regime)
    synthesis_system = _SYNTHESIS_SYSTEM.format(
        budget=budget,
        single_cap=int(budget * 0.30),
        beat_w=beat_w,
        guidance_w=guidance_w,
        setup_w=setup_w,
        fundamentals_w=fundamentals_w,
        max_score=max_score,
        strong_thresh=strong_thresh,
        strong_minus=strong_thresh - 1,
        moderate_thresh=moderate_thresh,
        weak_thresh=moderate_thresh - 1,
        short_thresh=short_thresh,
        im_cap_pct=IMPLIED_MOVE_LOSS_CAP * regime_mult * 100,
        im_cap=int(budget * IMPLIED_MOVE_LOSS_CAP * regime_mult),
        vix_risk_off=VIX_RISK_OFF,
    )
    synthesis_human = _SYNTHESIS_HUMAN.format(
        budget=budget,
        trade_date=trade_date,
        regime_block=format_regime(regime),
        ticker_sections=ticker_sections,
        perspectives=perspectives_block,
        reviews=reviews_block,
        lessons_block=lessons_block or "_No lessons yet — run more trades and use `tradingagents reflect` after each exit._",
    )

    report = _call(llm, synthesis_system, synthesis_human)

    # ── Step 5: deterministic constraint check + one corrective re-prompt ─────
    _log("Validating allocation constraints...")
    violations = validate_allocation(
        parse_allocation(report), budget, ticker_contexts,
        short_threshold=short_thresh, regime=regime,
    )
    if violations:
        _log(f"  {len(violations)} violation(s) found; requesting one corrected pass...")
        correction_human = _CORRECTION_HUMAN.format(
            report=report,
            violations="\n".join(f"- {v}" for v in violations),
        )
        try:
            corrected = _call(llm, synthesis_system, correction_human)
            corrected_violations = validate_allocation(
                parse_allocation(corrected), budget, ticker_contexts,
                short_threshold=short_thresh, regime=regime,
            )
            # Keep the original report if the corrective pass made things worse
            if len(corrected_violations) < len(violations):
                report, violations = corrected, corrected_violations
        except Exception as exc:
            _log(f"  Corrective pass failed: {exc}")

    if violations:
        _log(f"  {len(violations)} violation(s) remain; appending Constraint Check section.")
        report += format_violations(violations)
    else:
        _log("  Constraint check passed.")

    # Soft sizing advisories (#14b asymmetry + #14c crowding) — downgrade, don't skip.
    final_alloc = parse_allocation(report)
    advisories = (
        asymmetry_advisories(final_alloc, ticker_contexts)
        + crowding_advisories(final_alloc, ticker_contexts)
    )
    if advisories:
        _log(f"  {len(advisories)} sizing advisory(ies) appended.")
        report += format_advisories(advisories)

    _log("Council complete.")
    return report
