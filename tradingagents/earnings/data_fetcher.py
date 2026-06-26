"""Fetches earnings-specific data from yfinance for the EarningsLayer."""

from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

from tradingagents.dataflows.stockstats_utils import yf_retry
from tradingagents.dataflows.yfinance_news import get_news_yfinance
from tradingagents.earnings.peers import build_peer_readthrough, format_peer_readthrough


def _safe(fn, fallback="Not available"):
    try:
        return fn()
    except Exception:
        return fallback


def _df_to_str(df) -> str:
    if df is None or (hasattr(df, "empty") and df.empty):
        return "Not available"
    return df.to_string()


def fetch_earnings_context(ticker: str, analysis_date: str, news_lookback_days: int = 90) -> dict:
    """Return a dict of earnings-relevant data for the given ticker and date."""
    stock = yf.Ticker(ticker)

    # --- Earnings date ---
    def _earnings_date():
        cal = yf_retry(lambda: stock.calendar)
        if not cal:
            return "Not found"
        # calendar is a dict; Earnings Date may be a list of datetimes or a single value
        ed = cal.get("Earnings Date")
        if ed is None:
            return "Not found"
        if isinstance(ed, (list, tuple)):
            dates = [str(d)[:10] for d in ed if d is not None]
            return ", ".join(dates) if dates else "Not found"
        return str(ed)[:10]

    earnings_date = _safe(_earnings_date)

    # --- EPS estimates ---
    eps_estimates = _safe(lambda: _df_to_str(yf_retry(lambda: stock.earnings_estimate)))

    # --- Revenue estimates ---
    revenue_estimates = _safe(lambda: _df_to_str(yf_retry(lambda: stock.revenue_estimate)))

    # --- Historical beat / miss (last 8 quarters) ---
    def _earnings_history():
        hist = yf_retry(lambda: stock.earnings_history)
        if hist is None or (hasattr(hist, "empty") and hist.empty):
            return "Not available"
        # Keep the most recent 8 entries
        return _df_to_str(hist.tail(8))

    earnings_history = _safe(_earnings_history)

    # --- Analyst upgrades / downgrades (last 90 days) ---
    def _analyst_ratings():
        upgrades = yf_retry(lambda: stock.upgrades_downgrades)
        if upgrades is None or (hasattr(upgrades, "empty") and upgrades.empty):
            return "No recent changes"
        cutoff = datetime.strptime(analysis_date, "%Y-%m-%d") - timedelta(days=news_lookback_days)
        upgrades.index = pd.to_datetime(upgrades.index, utc=True).tz_localize(None)
        recent = upgrades[upgrades.index >= cutoff]
        if recent.empty:
            return "No analyst rating changes in the past 90 days"
        return _df_to_str(recent)

    analyst_ratings = _safe(_analyst_ratings)

    # --- Peer earnings read-through (#9) ---
    # Industry peers that already reported this season — the strongest free signal.
    def _peers():
        as_of = datetime.strptime(analysis_date, "%Y-%m-%d").date()
        return build_peer_readthrough(ticker, today=as_of)

    peer_data = _safe(_peers, fallback={})
    peer_readthrough = format_peer_readthrough(peer_data) if peer_data else "Not available"

    # --- News (configurable lookback window) ---
    lookback_start = (
        datetime.strptime(analysis_date, "%Y-%m-%d") - timedelta(days=news_lookback_days)
    ).strftime("%Y-%m-%d")
    recent_news = _safe(
        lambda: get_news_yfinance(ticker, lookback_start, analysis_date),
        fallback=f"No news available for {ticker}",
    )

    return {
        "earnings_date": earnings_date,
        "eps_estimates": eps_estimates,
        "revenue_estimates": revenue_estimates,
        "earnings_history": earnings_history,
        "analyst_ratings": analyst_ratings,
        "peer_readthrough": peer_readthrough,
        "peer_data": peer_data,
        "recent_news": recent_news,
        "news_lookback_days": news_lookback_days,
    }
