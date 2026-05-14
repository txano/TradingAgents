import os
import time
import logging
import threading

import pandas as pd
import yfinance as yf
from yfinance.exceptions import YFRateLimitError
from stockstats import wrap
from typing import Annotated
from .config import get_config
from .utils import safe_ticker_component

logger = logging.getLogger(__name__)

# Per-symbol locks prevent parallel workers from downloading the same ticker
# simultaneously and racing on the same cache file.
_CACHE_LOCKS: dict[str, threading.Lock] = {}
_CACHE_LOCKS_MUTEX = threading.Lock()


def _get_cache_lock(symbol: str) -> threading.Lock:
    with _CACHE_LOCKS_MUTEX:
        if symbol not in _CACHE_LOCKS:
            _CACHE_LOCKS[symbol] = threading.Lock()
        return _CACHE_LOCKS[symbol]


def yf_retry(func, max_retries=3, base_delay=2.0):
    """Execute a yfinance call with exponential backoff on rate limits.

    yfinance raises YFRateLimitError on HTTP 429 responses but does not
    retry them internally. This wrapper adds retry logic specifically
    for rate limits. Other exceptions propagate immediately.
    """
    for attempt in range(max_retries + 1):
        try:
            return func()
        except YFRateLimitError:
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(f"Yahoo Finance rate limited, retrying in {delay:.0f}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(delay)
            else:
                raise


def _normalize_columns(data: pd.DataFrame) -> pd.DataFrame:
    """Flatten MultiIndex columns, fix Datetime→Date, drop duplicates."""
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = [
            c[0] if isinstance(c, tuple) and c[1] == "" else "_".join(str(x) for x in c if x)
            for c in data.columns
        ]
    # yfinance uses 'Datetime' for intraday; normalize to 'Date'
    if "Datetime" in data.columns and "Date" not in data.columns:
        data = data.rename(columns={"Datetime": "Date"})
    # Drop duplicate column names — stockstats refuses non-unique columns
    data = data.loc[:, ~data.columns.duplicated()]
    return data


def _clean_dataframe(data: pd.DataFrame) -> pd.DataFrame:
    """Normalize a stock DataFrame for stockstats: parse dates, drop invalid rows, fill price gaps."""
    data = _normalize_columns(data).copy()
    data["Date"] = pd.to_datetime(data["Date"], errors="coerce")
    data = data.dropna(subset=["Date"]).copy()

    price_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in data.columns]
    data.loc[:, price_cols] = data[price_cols].apply(pd.to_numeric, errors="coerce")
    data = data.dropna(subset=["Close"]).copy()
    data.loc[:, price_cols] = data[price_cols].ffill().bfill()

    return data


def load_ohlcv(symbol: str, curr_date: str) -> pd.DataFrame:
    """Fetch OHLCV data with caching, filtered to prevent look-ahead bias.

    Downloads 5 years of data up to today and caches per symbol. On
    subsequent calls the cache is reused. Rows after curr_date are
    filtered out so backtests never see future prices.

    The per-symbol lock ensures that parallel workers for the same ticker
    don't race on the cache file.  Writes are atomic (temp-file + rename)
    so a partially-written file is never visible to other threads.
    """
    # Reject ticker values that would escape the cache directory when
    # interpolated into the cache filename (e.g. ``../../tmp/x``).
    safe_symbol = safe_ticker_component(symbol)

    config = get_config()
    curr_date_dt = pd.to_datetime(curr_date)

    today_date = pd.Timestamp.today()
    start_date = today_date - pd.DateOffset(years=5)
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = today_date.strftime("%Y-%m-%d")

    os.makedirs(config["data_cache_dir"], exist_ok=True)
    data_file = os.path.join(
        config["data_cache_dir"],
        f"{safe_symbol}-YFin-data-{start_str}-{end_str}.csv",
    )

    with _get_cache_lock(symbol):
        data = None

        if os.path.exists(data_file):
            try:
                data = pd.read_csv(data_file, on_bad_lines="skip", encoding="utf-8")
                data = _normalize_columns(data)
                if data.empty or "Date" not in data.columns:
                    data = None  # corrupt or empty — re-download below
            except Exception:
                data = None
            if data is None:
                try:
                    os.remove(data_file)
                except OSError:
                    pass

        if data is None:
            raw = yf_retry(lambda: yf.download(
                symbol,
                start=start_str,
                end=end_str,
                multi_level_index=False,
                progress=False,
                auto_adjust=True,
            ))
            data = _normalize_columns(raw.reset_index())
            # Atomic write: write to .tmp then rename so no reader ever sees
            # a partially-written file.
            tmp_file = data_file + ".tmp"
            try:
                data.to_csv(tmp_file, index=False, encoding="utf-8")
                os.replace(tmp_file, data_file)
            except Exception:
                try:
                    os.remove(tmp_file)
                except OSError:
                    pass

    data = _clean_dataframe(data)
    data = data[data["Date"] <= curr_date_dt]
    return data


def filter_financials_by_date(data: pd.DataFrame, curr_date: str) -> pd.DataFrame:
    """Drop financial statement columns (fiscal period timestamps) after curr_date.

    yfinance financial statements use fiscal period end dates as columns.
    Columns after curr_date represent future data and are removed to
    prevent look-ahead bias.
    """
    if not curr_date or data.empty:
        return data
    cutoff = pd.Timestamp(curr_date)
    mask = pd.to_datetime(data.columns, errors="coerce") <= cutoff
    return data.loc[:, mask]


class StockstatsUtils:
    @staticmethod
    def get_stock_stats(
        symbol: Annotated[str, "ticker symbol for the company"],
        indicator: Annotated[
            str, "quantitative indicators based off of the stock data for the company"
        ],
        curr_date: Annotated[
            str, "curr date for retrieving stock price data, YYYY-mm-dd"
        ],
    ):
        data = load_ohlcv(symbol, curr_date)
        df = wrap(data)
        df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
        curr_date_str = pd.to_datetime(curr_date).strftime("%Y-%m-%d")

        df[indicator]  # trigger stockstats to calculate the indicator
        matching_rows = df[df["Date"].str.startswith(curr_date_str)]

        if not matching_rows.empty:
            indicator_value = matching_rows[indicator].values[0]
            return indicator_value
        else:
            return "N/A: Not a trading day (weekend or holiday)"
