"""LLM agent that produces the portfolio allocation report."""

_SYSTEM_PROMPT = """You are a portfolio allocation manager at a hedge fund. You receive \
pre-earnings analysis briefs for a batch of stocks reporting earnings this week and must decide \
how to allocate a fixed capital budget across them.

Your goal is risk-adjusted returns, not maximum deployment. Leave capital in cash rather than \
force marginal trades.

Rules:
- Total budget: ${budget:,}
- Positions are BUY (long before earnings) or SHORT (short before earnings)
- Allocate $0 to any ticker where the edge is unclear or the risk/reward is unattractive
- Size proportionally to conviction: High confidence + strong absolute score → larger allocation
- Single position cap: 30% of budget (${single_cap:,})
- Avoid concentrating heavily in multiple stocks from the same sector
- Cash not deployed is fine — report it explicitly

For each ticker you are given:
- Scores: beat_score, guidance_score, setup_score, total_score (-15 to +15), signal, confidence
- The portfolio manager's final trading decision
- The earnings brief summary

Respond ONLY with the formatted report below. No preamble or commentary outside the template.

---
## Portfolio Allocation — {trade_date}
**Total Budget:** ${budget:,}

---
### Rationale

[3–5 sentences: which signals were strong enough to act on, what you passed on and why, \
and how you balanced long vs. short exposure overall.]

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
      "rationale": "<one sentence max 100 chars>"
    }}
  ]
}}
```
---
"""

_HUMAN_TEMPLATE = """Budget: ${budget:,}
Analysis Date: {trade_date}
Tickers: {n_tickers}

=== TICKER ANALYSES ===

{ticker_sections}
---
Produce the Portfolio Allocation Report now.
"""

_TICKER_SECTION_TEMPLATE = """### {ticker}
Sector: {sector}
Scores: beat={beat_score:+d}  guidance={guidance_score:+d}  setup={setup_score:+d}  total={total_score:+d}
Signal: {signal} | Confidence: {confidence} | Earnings: {earnings_date}
One-liner: {one_liner}

Portfolio Manager Decision:
{pm_decision}

Earnings Brief Summary:
{brief_summary}
"""


def create_allocation_analyst(llm, budget: int, trade_date: str):
    """Return a callable that generates the allocation report.

    Args:
        llm: Any LangChain-compatible chat model.
        budget: Total capital to allocate in dollars.
        trade_date: Analysis date (YYYY-MM-DD).
    """
    system_prompt = _SYSTEM_PROMPT.format(
        budget=budget,
        single_cap=int(budget * 0.30),
        trade_date=trade_date,
    )

    def run(ticker_contexts: list[dict]) -> str:
        sections = []
        for ctx in ticker_contexts:
            sections.append(
                _TICKER_SECTION_TEMPLATE.format(
                    ticker=ctx["ticker"],
                    sector=ctx.get("sector", "Unknown"),
                    beat_score=ctx.get("beat_score", 0),
                    guidance_score=ctx.get("guidance_score", 0),
                    setup_score=ctx.get("setup_score", 0),
                    total_score=ctx.get("total_score", 0),
                    signal=ctx.get("signal", "?"),
                    confidence=ctx.get("confidence", "?"),
                    earnings_date=ctx.get("earnings_date", "unknown"),
                    one_liner=ctx.get("one_liner", ""),
                    pm_decision=ctx.get("pm_decision", "Not available"),
                    brief_summary=ctx.get("brief_summary", "Not available"),
                )
            )

        messages = [
            ("system", system_prompt),
            (
                "human",
                _HUMAN_TEMPLATE.format(
                    budget=budget,
                    trade_date=trade_date,
                    n_tickers=len(ticker_contexts),
                    ticker_sections="\n".join(sections),
                ),
            ),
        ]
        return llm.invoke(messages).content

    return run
