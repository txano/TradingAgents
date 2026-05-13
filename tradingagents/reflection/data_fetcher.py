"""Fetch post-trade data for reflection analysis."""

from datetime import datetime, timedelta

import yfinance as yf


def fetch_reflection_context(ticker: str, trade_date: str, exit_date: str = None) -> dict:
    """Fetch actual earnings results, price action, and news for post-mortem analysis.

    Args:
        ticker: Stock ticker symbol.
        trade_date: Date trade was entered (YYYY-MM-DD).
        exit_date: Date trade was closed (YYYY-MM-DD). Defaults to today.

    Returns:
        dict with keys: actual_earnings, price_action, recent_news.
    """
    from datetime import date

    if exit_date is None:
        exit_date = date.today().isoformat()

    stock = yf.Ticker(ticker)

    # Actual earnings history — shows recent EPS actuals vs. estimates
    actual_earnings = "Not available"
    try:
        hist = stock.earnings_history
        if hist is not None and not hist.empty:
            actual_earnings = hist.tail(6).to_string()
    except Exception as e:
        actual_earnings = f"Could not fetch: {e}"

    # Price action from 2 days before entry to 3 days after exit
    price_action = "Not available"
    try:
        start = (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d")
        end = (datetime.strptime(exit_date, "%Y-%m-%d") + timedelta(days=3)).strftime("%Y-%m-%d")
        hist_prices = stock.history(start=start, end=end)
        if not hist_prices.empty:
            price_action = hist_prices[["Open", "High", "Low", "Close", "Volume"]].to_string()
    except Exception as e:
        price_action = f"Could not fetch: {e}"

    # Recent news (best-effort; yfinance only returns current articles)
    recent_news = "Not available"
    try:
        news_items = stock.news or []
        parts = []
        for item in news_items[:10]:
            content_data = item.get("content", {})
            title = content_data.get("title", "No title")
            summary = content_data.get("summary", "")
            pub = content_data.get("pubDate", "")
            parts.append(f"**{title}** ({pub})\n{summary}")
        recent_news = "\n\n".join(parts) if parts else "No news found"
    except Exception as e:
        recent_news = f"Could not fetch: {e}"

    return {
        "actual_earnings": actual_earnings,
        "price_action": price_action,
        "recent_news": recent_news,
    }
