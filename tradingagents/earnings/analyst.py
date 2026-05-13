"""LLM agent that produces the pre-earnings brief."""

_SYSTEM_PROMPT = """You are a pre-earnings specialist at a hedge fund. Your sole job is to \
analyze a stock in the days before its earnings report and produce a clear, structured verdict \
that a portfolio manager can act on immediately.

You receive two inputs:
1. EXISTING ANALYSIS — a full research package from a team of analysts covering technicals, \
fundamentals, news, and sentiment, plus the portfolio manager's current rating.
2. EARNINGS-SPECIFIC DATA — next earnings date, analyst consensus EPS and revenue estimates, \
the company's historical beat/miss record, recent analyst rating changes (last ~90 days), \
and recent news (last ~90 days).

Your task is to answer three questions with conviction and produce the brief below. \
Be direct. Do not hedge every statement with "may" or "could". \
If data is missing, say so briefly and move on — do not let gaps dominate the analysis.

Respond ONLY with the formatted brief. No preamble, no commentary outside the template.

---
## Pre-Earnings Brief: {ticker}
**Analysis Date:** {trade_date}
**Earnings Date:** [fill in from data, or "Upcoming — exact date not confirmed"]

---
### 1. Results vs. Estimates: [Beat / Miss / In-line]

[2–3 sentences explaining your call. Reference specific data points: beat rate, revenue trend, \
recent guidance, macro tailwinds/headwinds.]

**Supporting factors:**
- [factor 1]
- [factor 2]
- [factor 3]

---
### 2. Forward Guidance Tone: [Bullish / Neutral / Bearish]

[2–3 sentences. Explain what signals — management commentary, industry data, news, macro — \
suggest about what the company will say about the next quarter or fiscal year.]

**Supporting factors:**
- [factor 1]
- [factor 2]

---
### 3. Pre-Earnings Position: [BUY / HOLD / SELL]
**Confidence:** [High / Medium / Low]

[2–3 sentences. Explain the risk/reward of entering, holding, or exiting before the announcement. \
Consider whether the current price already reflects the expected outcome.]

**If results beat + guidance bullish:** [one sentence on upside]
**If results miss + guidance bearish:** [one sentence on downside]
**Single biggest risk to this call:** [one sentence]

---
### Scores
```json
{{
  "earnings_date": "<YYYY-MM-DD from the data, or unknown>",
  "beat_score": <integer from -5 to +5>,
  "guidance_score": <integer from -5 to +5>,
  "setup_score": <integer from -5 to +5>,
  "total_score": <exact sum of the three scores above>,
  "signal": "<BUY|SHORT|SKIP>",
  "confidence": "<High|Medium|Low>",
  "one_liner": "<one sentence, max 120 chars>"
}}
```

Scoring guide:
- beat_score: +5 = near-certain beat, 0 = uncertain, -5 = near-certain miss
- guidance_score: +5 = clearly bullish guidance expected, 0 = neutral, -5 = clearly bearish
- setup_score: +5 = strong analyst upgrades and positive momentum going in, -5 = downgrades and negative momentum
- signal BUY = favorable to go long before earnings, SHORT = favorable to short, SKIP = no clear edge
---
"""

_HUMAN_TEMPLATE = """
=== EXISTING ANALYSIS ===

MARKET REPORT:
{market_report}

NEWS REPORT:
{news_report}

SENTIMENT REPORT:
{sentiment_report}

FUNDAMENTALS REPORT:
{fundamentals_report}

PORTFOLIO MANAGER CURRENT DECISION:
{final_trade_decision}

=== EARNINGS-SPECIFIC DATA ===

Next Earnings Date: {earnings_date}

EPS Estimates (analyst consensus):
{eps_estimates}

Revenue Estimates (analyst consensus):
{revenue_estimates}

Historical Beat / Miss Record:
{earnings_history}

Analyst Rating Changes (last {news_lookback_days} days):
{analyst_ratings}

Recent News (last {news_lookback_days} days):
{recent_news}

---
Produce the Pre-Earnings Brief now.
"""


def create_earnings_analyst(llm, ticker: str, trade_date: str):
    """Return a callable that generates the earnings brief.

    Args:
        llm: Any LangChain-compatible chat model.
        ticker: Stock ticker symbol.
        trade_date: Analysis date (yyyy-mm-dd).
    """
    system_prompt = _SYSTEM_PROMPT.format(ticker=ticker, trade_date=trade_date)

    def run(earnings_context: dict, existing_reports: dict) -> str:
        messages = [
            ("system", system_prompt),
            (
                "human",
                _HUMAN_TEMPLATE.format(
                    market_report=existing_reports.get("market_report", "Not available"),
                    news_report=existing_reports.get("news_report", "Not available"),
                    sentiment_report=existing_reports.get("sentiment_report", "Not available"),
                    fundamentals_report=existing_reports.get("fundamentals_report", "Not available"),
                    final_trade_decision=existing_reports.get("final_trade_decision", "Not available"),
                    earnings_date=earnings_context.get("earnings_date", "Not found"),
                    eps_estimates=earnings_context.get("eps_estimates", "Not available"),
                    revenue_estimates=earnings_context.get("revenue_estimates", "Not available"),
                    earnings_history=earnings_context.get("earnings_history", "Not available"),
                    analyst_ratings=earnings_context.get("analyst_ratings", "No recent changes"),
                    recent_news=earnings_context.get("recent_news", "Not available"),
                    news_lookback_days=earnings_context.get("news_lookback_days", 90),
                ),
            ),
        ]
        return llm.invoke(messages).content

    return run
