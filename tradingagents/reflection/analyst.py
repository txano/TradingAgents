"""LLM agent that produces the trade post-mortem."""

_SYSTEM_PROMPT = """You are a trade performance analyst at a hedge fund. Your job is to review \
completed trades, compare them against the pre-trade analysis, and extract lessons that will \
improve future trading decisions.

You receive:
1. TRADE DETAILS — ticker, direction, entry/exit prices, and P&L
2. PRE-TRADE ANALYSIS — the earnings brief and final trading decision that drove the trade
3. POST-TRADE DATA — actual earnings results, price action during the trade, and recent news

Your task:
- Compare what was predicted vs. what actually happened
- Identify what the analysis got right and what it missed
- Extract specific, actionable lessons
- Be direct and reference actual numbers
- Do not be defensive about wrong predictions — honest analysis is more valuable

Respond ONLY with the formatted post-mortem. No preamble, no commentary outside the template.

---
## Trade Post-Mortem: {ticker}
**Trade Date:** {trade_date}
**Exit Date:** {exit_date}
**Direction:** {direction}
**Entry:** ${entry_price} → **Exit:** ${exit_price}
**P&L:** {pnl_display}

---
### 1. What Actually Happened

[2–3 sentences: actual earnings results vs. estimates, how the stock reacted on and after the \
announcement, and the key market narrative post-announcement. Include specific numbers.]

---
### 2. Prediction Accuracy

**Beat/Miss Prediction:**
[Was the beat_score directionally correct? What did the pre-trade brief predict, and what \
actually happened? Quote specific EPS/revenue figures if available.]

**Guidance Prediction:**
[Was the guidance_score correct? What did management actually say vs. the prediction?]

**Setup/Technical Prediction:**
[Was the setup_score correct? Did analyst sentiment and momentum play out as expected?]

---
### 3. Root Cause Analysis

[2–3 sentences: WHY was the analysis right or wrong? Was it a data gap, a model bias, an \
unexpected macro event, or execution timing?]

**What the analysis missed:**
- [specific gap 1]
- [specific gap 2]

**What the analysis got right:**
- [specific hit 1]
- [specific hit 2]

---
### 4. Lessons for Future Analyses

1. [Actionable lesson — specific enough to change how we analyze the next similar stock]
2. [Actionable lesson 2]
3. [Actionable lesson 3, if applicable — omit if not applicable]

---
### Post-Mortem Score
```json
{{
  "ticker": "<ticker symbol>",
  "trade_date": "<YYYY-MM-DD>",
  "direction": "<BUY|SHORT>",
  "pnl_pct": <float — actual percentage P&L>,
  "outcome": "<WIN|LOSS|BREAKEVEN>",
  "prediction_accuracy": "<Correct|Partial|Incorrect>",
  "beat_prediction_correct": <true|false|null — null if earnings not in the period>,
  "guidance_prediction_correct": <true|false|null>,
  "key_lesson": "<one sentence, max 120 chars>"
}}
```
---
"""

_HUMAN_TEMPLATE = """
=== TRADE DETAILS ===
Ticker: {ticker}
Direction: {direction}
Shares: {shares}
Entry Price: ${entry_price}
Exit Price: ${exit_price}
P&L: {pnl_display} ({pnl_pct:+.1f}%)
Trade Date: {trade_date}
Exit Date: {exit_date}

=== PRE-TRADE ANALYSIS ===

EARNINGS BRIEF (written before the announcement):
{earnings_brief}

FINAL TRADING DECISION (from the research team):
{trade_decision}

=== POST-TRADE DATA ===

ACTUAL EARNINGS RESULTS:
{actual_earnings}

PRICE ACTION DURING THE TRADE:
{price_action}

RECENT NEWS:
{recent_news}

---
Produce the Trade Post-Mortem now.
"""


def create_reflection_analyst(
    llm,
    ticker: str,
    trade_date: str,
    exit_date: str,
    direction: str,
    entry_price: float,
    exit_price: float,
    shares: float,
):
    """Return a callable that generates the trade post-mortem.

    Args:
        llm: Any LangChain-compatible chat model.
        ticker: Stock ticker symbol.
        trade_date: Date trade was entered (YYYY-MM-DD).
        exit_date: Date trade was closed (YYYY-MM-DD).
        direction: "BUY" or "SHORT".
        entry_price: Entry price per share.
        exit_price: Exit price per share.
        shares: Number of shares traded.
    """
    pnl = (exit_price - entry_price) * shares if direction == "BUY" else (entry_price - exit_price) * shares
    pnl_pct = ((exit_price - entry_price) / entry_price * 100) if direction == "BUY" else ((entry_price - exit_price) / entry_price * 100)
    pnl_display = f"${pnl:+.2f} ({pnl_pct:+.1f}%)"

    system_prompt = _SYSTEM_PROMPT.format(
        ticker=ticker,
        trade_date=trade_date,
        exit_date=exit_date,
        direction=direction,
        entry_price=f"{entry_price:.2f}",
        exit_price=f"{exit_price:.2f}",
        pnl_display=pnl_display,
    )

    def run(reflection_context: dict, prior_analysis: dict) -> str:
        messages = [
            ("system", system_prompt),
            (
                "human",
                _HUMAN_TEMPLATE.format(
                    ticker=ticker,
                    direction=direction,
                    shares=shares,
                    entry_price=f"{entry_price:.2f}",
                    exit_price=f"{exit_price:.2f}",
                    pnl_display=pnl_display,
                    pnl_pct=pnl_pct,
                    trade_date=trade_date,
                    exit_date=exit_date,
                    earnings_brief=prior_analysis.get("earnings_brief", "Not available"),
                    trade_decision=prior_analysis.get("final_trade_decision", "Not available"),
                    actual_earnings=reflection_context.get("actual_earnings", "Not available"),
                    price_action=reflection_context.get("price_action", "Not available"),
                    recent_news=reflection_context.get("recent_news", "Not available"),
                ),
            ),
        ]
        return llm.invoke(messages).content

    return run
