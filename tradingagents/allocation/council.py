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

_REVIEW_SYSTEM = (
    "You are an objective meta-analyst reviewing investment perspectives from five "
    "anonymous advisors. Your role is to evaluate the quality of their reasoning "
    "and identify what the group collectively got right, wrong, or missed entirely."
)

_REVIEW_HUMAN = """\
Five advisors independently reviewed the same set of pre-earnings tickers. \
Their perspectives are anonymized below.

=== ADVISOR PERSPECTIVES ===
{perspectives}

---
Answer each question directly and specifically (reference tickers or arguments where relevant):

**1. Strongest perspective**
Which advisor (Alpha/Beta/Gamma/Delta/Epsilon) made the strongest overall case \
and why? (2 sentences)

**2. Biggest blind spot**
Which advisor had the most significant blind spot or missed risk, and what was it? \
(2 sentences)

**3. Collective gap**
What did ALL FIVE advisors miss or underweight — something none of them addressed \
adequately? (2-3 sentences)
"""

_SYNTHESIS_SYSTEM = """\
You are a senior portfolio manager making final capital allocation decisions. \
You have five independent analytical perspectives on a set of pre-earnings trades, \
plus a meta-review from each advisor evaluating all five. Your job is to synthesize \
this into a rigorous, actionable allocation.

Core rules:
- Total budget: ${budget:,}
- Single position cap: 30% of budget (${single_cap:,})
- No sector may exceed 35% of deployed capital
- SHORTS require High conviction only AND weighted_score ≤ -3
- Sizing tiers (% of budget):
    High conviction  → 15–25%
    Medium conviction → 7–14%
    Low conviction   → ≤ 6% or SKIP
- Score interpretation:
    weighted_score ≥ +8  → strong long signal
    +4 to +7             → moderate long signal
    ≤ +3                 → weak; skip unless compelling brief
    ≤ -3                 → short candidate (High conviction only)
- Weights used: beat={beat_w:.2f} × beat_score + guidance={guidance_w:.2f} × \
guidance_score + setup={setup_w:.2f} × setup_score
  Higher weight = historically more predictive bucket per calibration data.
- Cash not deployed is acceptable; never force marginal trades.
"""

_SYNTHESIS_HUMAN = """\
Budget: ${budget:,}
Analysis Date: {trade_date}

=== TICKER DATA ===
{ticker_sections}

=== FIVE ADVISOR PERSPECTIVES ===
{perspectives}

=== CROSS-REVIEWS ===
{reviews}

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

# ── Helpers ───────────────────────────────────────────────────────────────────

def _cut(text: str, max_chars: int) -> str:
    """Truncate at the last sentence boundary within max_chars."""
    if len(text) <= max_chars:
        return text
    chunk = text[:max_chars]
    last = max(chunk.rfind("."), chunk.rfind("!"), chunk.rfind("?"))
    if last > max_chars // 2:
        return chunk[: last + 1].strip()
    return chunk.strip() + "…"


_TICKER_SECTION = """\
### {ticker}
Sector: {sector} | Earnings: {earnings_date}
Scores (raw):     beat={beat_score:+d}  guidance={guidance_score:+d}  setup={setup_score:+d}  total={total_score:+d}
Scores (weighted, w_beat={beat_weight:.2f} w_guidance={guidance_weight:.2f} w_setup={setup_weight:.2f}):  weighted={weighted_score:+.1f}
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
                beat_weight=ctx.get("beat_weight", 1.0),
                guidance_weight=ctx.get("guidance_weight", 1.0),
                setup_weight=ctx.get("setup_weight", 1.0),
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
    progress_cb=None,
) -> str:
    """Run the full council pipeline and return the final allocation report.

    Args:
        llm: LangChain-compatible chat model.
        ticker_contexts: Enriched list of dicts from AllocationLayer._build_contexts().
        budget: Total capital in dollars.
        trade_date: YYYY-MM-DD string.
        weights: Dict with beat/guidance/setup floats from weights.py.
        progress_cb: Optional callable(message: str) for progress updates.
    """
    beat_w     = weights.get("beat",     1.0)
    guidance_w = weights.get("guidance", 1.0)
    setup_w    = weights.get("setup",    1.0)

    ticker_sections = _format_sections(ticker_contexts)

    def _log(msg: str) -> None:
        if progress_cb:
            progress_cb(msg)

    # ── Step 1: five advisors in parallel ──────────────────────────────────────
    _log("Consulting advisors (5 parallel)...")
    advisor_responses: dict[str, str] = {}

    def _run_advisor(advisor: dict) -> tuple[str, str]:
        human = _ADVISOR_HUMAN.format(
            budget=budget,
            trade_date=trade_date,
            ticker_sections=ticker_sections,
        )
        return advisor["label"], _call(llm, advisor["system"], human)

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_run_advisor, a): a["label"] for a in _ADVISORS}
        for fut in as_completed(futures):
            label = futures[fut]
            try:
                lbl, resp = fut.result()
                advisor_responses[lbl] = resp
                _log(f"  Advisor {lbl} ({_ADVISORS[[a['label'] for a in _ADVISORS].index(lbl)]['name']}) done.")
            except Exception as exc:
                advisor_responses[label] = f"[Advisor {label} failed: {exc}]"
                _log(f"  Advisor {label} failed: {exc}")

    # ── Step 2: format anonymized perspectives ─────────────────────────────────
    _log("Running cross-reviews (5 parallel)...")
    perspectives_block = "\n\n".join(
        f"--- Advisor {lbl} ---\n{advisor_responses.get(lbl, '[no response]')}"
        for lbl in ["Alpha", "Beta", "Gamma", "Delta", "Epsilon"]
    )

    # ── Step 3: five cross-reviews in parallel ─────────────────────────────────
    review_responses: dict[str, str] = {}

    def _run_review(advisor: dict) -> tuple[str, str]:
        human = _REVIEW_HUMAN.format(perspectives=perspectives_block)
        return advisor["label"], _call(llm, _REVIEW_SYSTEM, human)

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_run_review, a): a["label"] for a in _ADVISORS}
        for fut in as_completed(futures):
            label = futures[fut]
            try:
                lbl, resp = fut.result()
                review_responses[lbl] = resp
                _log(f"  Review from {lbl} done.")
            except Exception as exc:
                review_responses[label] = f"[Review {label} failed: {exc}]"

    # ── Step 4: synthesis ──────────────────────────────────────────────────────
    _log("Synthesizing final allocation...")
    reviews_block = "\n\n".join(
        f"--- {lbl}'s review ---\n{review_responses.get(lbl, '[no response]')}"
        for lbl in ["Alpha", "Beta", "Gamma", "Delta", "Epsilon"]
    )

    synthesis_system = _SYNTHESIS_SYSTEM.format(
        budget=budget,
        single_cap=int(budget * 0.30),
        beat_w=beat_w,
        guidance_w=guidance_w,
        setup_w=setup_w,
    )
    synthesis_human = _SYNTHESIS_HUMAN.format(
        budget=budget,
        trade_date=trade_date,
        ticker_sections=ticker_sections,
        perspectives=perspectives_block,
        reviews=reviews_block,
    )

    report = _call(llm, synthesis_system, synthesis_human)
    _log("Council complete.")
    return report
