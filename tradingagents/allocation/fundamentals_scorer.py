"""LLM-based fundamentals quality scorer for the allocation layer.

Reads an existing fundamentals.md analyst report (produced by the LangGraph
fundamentals analyst) and converts it into a structured quality score that the
allocation council can use as a 4th decision dimension alongside beat/guidance/setup.

Usage:
    from tradingagents.allocation.fundamentals_scorer import score_fundamentals

    score = score_fundamentals(llm, fundamentals_md_text, "NVDA")
    # {"fundamentals_score": 3, "balance_sheet": "Strong",
    #  "profitability": "Expanding", "growth_quality": "High", "summary": "..."}
"""

import json
import logging
import re

logger = logging.getLogger(__name__)

_SCORE_SYSTEM = """\
You are a CFA-level financial analyst. You will be given the fundamentals analyst \
report for a stock (written by an AI analyst), and — when available — a set of \
COMPUTED METRICS derived directly from the company's financial statements. \
Treat the computed metrics as ground truth: when the analyst report conflicts \
with them, trust the metrics. Your job is to extract a concise, structured \
quality assessment of the company's fundamental health.

Scoring scale for fundamentals_score (-5 to +5):
  +5  Fortress: pristine balance sheet, expanding margins, high-quality FCF growth, \
clear competitive moat
  +3  Quality: solid financials, stable-to-growing margins, manageable debt
  +1  Adequate: average business, no red flags, limited differentiation
   0  Neutral: mixed signals, some positives and negatives roughly balanced
  -1  Caution: early signs of stress (leverage increasing, margins compressing, \
slowing growth)
  -3  Weak: clear deterioration (rising debt, shrinking margins, negative FCF trend)
  -5  Distressed: leveraged, burning cash, declining revenue, possible solvency risk

Output ONLY a JSON object — no other text:
{
  "fundamentals_score": <integer -5 to +5>,
  "balance_sheet": "<Strong | Adequate | Weak>",
  "profitability": "<Expanding | Stable | Contracting>",
  "growth_quality": "<High | Medium | Low>",
  "summary": "<one sentence max 120 chars>"
}
"""

_SCORE_HUMAN = """\
Ticker: {ticker}
{metrics_block}
=== FUNDAMENTALS ANALYST REPORT ===
{fundamentals_md}

---
Assess the fundamental quality and return the JSON object described in your instructions.
"""

_FALLBACK = {
    "fundamentals_score": 0,
    "balance_sheet": "Adequate",
    "profitability": "Stable",
    "growth_quality": "Medium",
    "summary": "Fundamentals data not available — treated as neutral.",
}


def fetch_fundamental_metrics(ticker: str) -> dict | None:
    """Compute hard fundamental metrics from yfinance financial statements.

    Grounds the LLM score in real numbers instead of letting one model grade
    another model's prose. Every field is individually guarded; returns None
    when nothing could be computed (offline, delisted, missing statements).
    """
    try:
        import yfinance as yf
        from tradingagents.dataflows.stockstats_utils import yf_retry
    except ImportError:
        return None

    def _safe(fn):
        try:
            return yf_retry(fn)
        except Exception:
            return None

    stock = yf.Ticker(ticker)
    income = _safe(lambda: stock.quarterly_income_stmt)
    cashflow = _safe(lambda: stock.quarterly_cashflow)
    info = _safe(lambda: stock.info) or {}

    def _series(df, name, limit=8):
        """Most-recent-first list of floats for a statement line item."""
        try:
            if df is not None and name in df.index:
                return [float(v) for v in df.loc[name].dropna().tolist()][:limit]
        except Exception:
            pass
        return []

    metrics: dict = {}

    revenue = _series(income, "Total Revenue")
    if len(revenue) >= 5 and revenue[4]:
        metrics["revenue_yoy_growth_pct"] = round((revenue[0] / revenue[4] - 1) * 100, 1)

    def _margin(series_name, key):
        series = _series(income, series_name)
        n = min(len(series), len(revenue), 4)
        if n >= 2 and revenue[0] and revenue[n - 1]:
            latest = series[0] / revenue[0] * 100
            oldest = series[n - 1] / revenue[n - 1] * 100
            metrics[f"{key}_pct"] = round(latest, 1)
            metrics[f"{key}_delta_pp"] = round(latest - oldest, 1)

    _margin("Gross Profit", "gross_margin")
    _margin("Operating Income", "operating_margin")

    ocf = _series(cashflow, "Operating Cash Flow")
    capex = _series(cashflow, "Capital Expenditure")  # negative in yfinance
    n = min(len(ocf), len(capex), 4)
    if n >= 2:
        fcf_quarters = [ocf[i] + capex[i] for i in range(n)]
        metrics["fcf_ttm"] = round(sum(fcf_quarters))
        metrics["fcf_latest_quarter"] = round(fcf_quarters[0])

    for info_key, key in [("totalDebt", "total_debt"), ("totalCash", "total_cash"),
                          ("ebitda", "ebitda_ttm")]:
        value = info.get(info_key)
        if isinstance(value, (int, float)):
            metrics[key] = value
    debt, ebitda = metrics.get("total_debt"), metrics.get("ebitda_ttm")
    if debt is not None and ebitda:
        metrics["debt_to_ebitda"] = round(debt / ebitda, 2)

    return metrics or None


def _format_metrics(metrics: dict) -> str:
    def _money(v):
        return f"${v / 1e9:,.2f}B" if abs(v) >= 1e9 else f"${v / 1e6:,.1f}M"

    lines = []
    if "revenue_yoy_growth_pct" in metrics:
        lines.append(f"Revenue growth (YoY, latest quarter): {metrics['revenue_yoy_growth_pct']:+.1f}%")
    if "gross_margin_pct" in metrics:
        lines.append(
            f"Gross margin: {metrics['gross_margin_pct']:.1f}% "
            f"({metrics['gross_margin_delta_pp']:+.1f}pp over last 4 quarters)"
        )
    if "operating_margin_pct" in metrics:
        lines.append(
            f"Operating margin: {metrics['operating_margin_pct']:.1f}% "
            f"({metrics['operating_margin_delta_pp']:+.1f}pp over last 4 quarters)"
        )
    if "fcf_ttm" in metrics:
        lines.append(
            f"Free cash flow: {_money(metrics['fcf_ttm'])} TTM "
            f"(latest quarter {_money(metrics['fcf_latest_quarter'])})"
        )
    if "total_debt" in metrics and "total_cash" in metrics:
        lines.append(f"Total debt: {_money(metrics['total_debt'])} vs cash: {_money(metrics['total_cash'])}")
    if "debt_to_ebitda" in metrics:
        lines.append(f"Debt / EBITDA (TTM): {metrics['debt_to_ebitda']:.2f}x")
    return "\n".join(lines)


def score_fundamentals(llm, fundamentals_md: str, ticker: str, metrics: dict | None = None) -> dict:
    """Score the fundamental quality of a ticker from its fundamentals analyst report.

    When `metrics` (from fetch_fundamental_metrics) is provided, it is included
    in the prompt as ground truth so the score is anchored to real statement
    data rather than prose alone.

    Returns a dict with: fundamentals_score (int), balance_sheet, profitability,
    growth_quality (str labels), and summary (str).
    Falls back to a neutral score on any failure so the pipeline never blocks.
    """
    has_report = bool(fundamentals_md and fundamentals_md.strip())
    if not has_report and not metrics:
        return dict(_FALLBACK, summary=f"{ticker}: no fundamentals report found.")

    metrics_block = ""
    if metrics:
        metrics_block = (
            "\n=== COMPUTED METRICS (ground truth, from financial statements) ===\n"
            + _format_metrics(metrics)
            + "\n"
        )

    human = _SCORE_HUMAN.format(
        ticker=ticker,
        metrics_block=metrics_block,
        fundamentals_md=fundamentals_md[:6000] if has_report else "Not available.",
    )

    try:
        response = llm.invoke([("system", _SCORE_SYSTEM), ("human", human)])
        raw = response.content
    except Exception as exc:
        logger.warning("%s: fundamentals scoring LLM call failed: %s", ticker, exc)
        return dict(_FALLBACK)

    result = _parse_json(raw)
    if result is None:
        logger.warning("%s: could not parse fundamentals score JSON from LLM output", ticker)
        return dict(_FALLBACK)

    result["fundamentals_score"] = max(-5, min(5, int(result.get("fundamentals_score", 0))))
    return result


def _parse_json(text: str) -> dict | None:
    """Extract the first JSON object from text, return None if not found/invalid."""
    match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
