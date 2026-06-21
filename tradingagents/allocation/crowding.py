"""Crowding / run-up gate (ROADMAP #14c, playbook Block B).

A beat that's already priced in is the classic "beat-and-fall" trap. This module
measures how crowded a name is going into the print:

  * run-up — 1m / 3m return, absolute and relative to its sector ETF (a big run
    means the beat is largely discounted; the single best predictor of beat-and-fall)
  * distance from the 52-week high — within a few % = limited upside surprise room,
    large air pocket below (asymmetry against a long)
  * revision momentum — a cluster of upward EPS-estimate revisions means the whisper
    sits above consensus, so the bar to clear is higher than the published number

These are **soft** signals (downgrade conviction, don't skip — #14b decision). All
network access is guarded; failures yield None fields, never an exception.
"""

from __future__ import annotations

import logging
import math
import threading
from datetime import date

logger = logging.getLogger(__name__)

# Sector → SPDR sector ETF for relative-strength comparison. Falls back to SPY.
SECTOR_ETF = {
    "Technology":             "XLK",
    "Information Technology":  "XLK",
    "Healthcare":             "XLV",
    "Health Care":            "XLV",
    "Financial Services":     "XLF",
    "Financials":             "XLF",
    "Consumer Cyclical":      "XLY",
    "Consumer Discretionary": "XLY",
    "Consumer Defensive":     "XLP",
    "Consumer Staples":       "XLP",
    "Energy":                 "XLE",
    "Industrials":            "XLI",
    "Basic Materials":        "XLB",
    "Materials":              "XLB",
    "Utilities":              "XLU",
    "Real Estate":            "XLRE",
    "Communication Services": "XLC",
}
_FALLBACK_ETF = "SPY"

# Thresholds — playbook priors; #17's backtest harness should recalibrate these.
RUNUP_ABS_MAX = 12.0         # 1m absolute return above this = crowded
RUNUP_VS_SECTOR_MAX = 8.0    # 1m return above the sector by this = crowded
DIST_52W_HIGH_MAX = 3.0      # within this % of the 52w high = poor asymmetry
REVISION_UP_COUNT = 3        # ≥ this many net up-revisions in 30d = whisper above consensus

# Tiny per-process, per-day cache so a screen of N same-sector names doesn't
# re-fetch the same ETF N times.
_ETF_CACHE: dict[tuple[str, str], dict] = {}
_ETF_LOCK = threading.Lock()


def _safe_float(val):
    if val is None:
        return None
    try:
        f = float(val)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _safe_int(val):
    f = _safe_float(val)
    return int(f) if f is not None else None


# ---------------------------------------------------------------------------
# Pure flag logic (unit-tested directly)
# ---------------------------------------------------------------------------

def compute_crowding_flags(c: dict) -> list[str]:
    """Human-readable flags for whichever crowding signals tripped (may be empty)."""
    flags: list[str] = []
    r1 = c.get("runup_1m_pct")
    r1s = c.get("runup_1m_vs_sector")
    dist = c.get("dist_52w_high_pct")
    up = c.get("revision_up_30d")
    down = c.get("revision_down_30d")

    if isinstance(r1, (int, float)) and r1 >= RUNUP_ABS_MAX:
        flags.append(f"1m run-up {r1:+.0f}%")
    if isinstance(r1s, (int, float)) and r1s >= RUNUP_VS_SECTOR_MAX:
        flags.append(f"{r1s:+.0f}% vs sector (1m)")
    if isinstance(dist, (int, float)) and dist <= DIST_52W_HIGH_MAX:
        flags.append(f"within {dist:.0f}% of 52w high")
    if isinstance(up, int) and up >= REVISION_UP_COUNT and (down is None or up > down):
        flags.append(f"{up} up-revisions/30d (whisper > consensus)")
    return flags


def format_crowding(c: dict | None) -> str:
    """One-line summary for the council ticker section."""
    if not c:
        return "Not available"

    def _na(v, fmt):
        return fmt.format(v) if isinstance(v, (int, float)) else "n/a"

    return (
        f"1m {_na(c.get('runup_1m_pct'), '{:+.0f}%')} "
        f"({_na(c.get('runup_1m_vs_sector'), '{:+.0f}%')} vs {c.get('sector_etf', '?')}) · "
        f"3m {_na(c.get('runup_3m_pct'), '{:+.0f}%')} · "
        f"{_na(c.get('dist_52w_high_pct'), '{:.0f}%')} below 52w high"
        + (f" | FLAGS: {', '.join(c['flags'])}" if c.get("flags") else "")
    )


# ---------------------------------------------------------------------------
# Data fetch (yfinance — guarded)
# ---------------------------------------------------------------------------

def _returns(closes, spans=(21, 63)) -> dict:
    """Trailing % returns over the given trading-day spans, newest bar last."""
    out = {}
    try:
        vals = [v for v in closes if v is not None and not math.isnan(v)]
    except TypeError:
        vals = list(closes)
    for n in spans:
        if len(vals) > n and vals[-1 - n]:
            out[n] = (vals[-1] / vals[-1 - n] - 1) * 100
        else:
            out[n] = None
    return out


def _etf_returns(etf: str, _safe) -> dict:
    """1m/3m returns for a sector ETF, cached per process per day."""
    key = (etf, date.today().isoformat())
    with _ETF_LOCK:
        if key in _ETF_CACHE:
            return _ETF_CACHE[key]
    import yfinance as yf
    hist = _safe(lambda: yf.Ticker(etf).history(period="4mo"))
    rets = {21: None, 63: None}
    if hist is not None and not getattr(hist, "empty", True):
        rets = _returns(list(hist["Close"]))
    with _ETF_LOCK:
        _ETF_CACHE[key] = rets
    return rets


def fetch_crowding(ticker: str, sector: str | None = None) -> dict:
    """Compute run-up, 52w-high distance, and revision momentum for a ticker."""
    out = {
        "sector_etf":          None,
        "runup_1m_pct":        None,
        "runup_3m_pct":        None,
        "runup_1m_vs_sector":  None,
        "runup_3m_vs_sector":  None,
        "dist_52w_high_pct":   None,
        "revision_up_30d":     None,
        "revision_down_30d":   None,
    }
    try:
        import yfinance as yf
        from tradingagents.dataflows.stockstats_utils import yf_retry
    except ImportError:
        return out

    def _safe(fn):
        try:
            return yf_retry(fn)
        except Exception:
            return None

    stock = yf.Ticker(ticker)

    hist = _safe(lambda: stock.history(period="4mo"))
    if hist is not None and not getattr(hist, "empty", True):
        rets = _returns(list(hist["Close"]))
        out["runup_1m_pct"] = _round(rets.get(21))
        out["runup_3m_pct"] = _round(rets.get(63))

    etf = SECTOR_ETF.get((sector or "").strip(), _FALLBACK_ETF)
    out["sector_etf"] = etf
    etf_rets = _etf_returns(etf, _safe)
    if out["runup_1m_pct"] is not None and etf_rets.get(21) is not None:
        out["runup_1m_vs_sector"] = _round(out["runup_1m_pct"] - etf_rets[21])
    if out["runup_3m_pct"] is not None and etf_rets.get(63) is not None:
        out["runup_3m_vs_sector"] = _round(out["runup_3m_pct"] - etf_rets[63])

    fast = _safe(lambda: stock.fast_info)
    if fast is not None:
        try:
            high = _safe_float(fast["year_high"])
            price = _safe_float(getattr(fast, "last_price", None) or fast["last_price"])
            if high and price and high > 0:
                out["dist_52w_high_pct"] = _round((high - price) / high * 100)
        except Exception:
            pass

    _revisions(stock, _safe, out)

    out["flags"] = compute_crowding_flags(out)
    return out


def _revisions(stock, _safe, out: dict) -> None:
    """Best-effort net up/down EPS-estimate revisions over the last 30 days."""
    rev = _safe(lambda: stock.eps_revisions)
    if rev is None or getattr(rev, "empty", True):
        return
    try:
        row = rev.loc["0q"] if "0q" in getattr(rev, "index", []) else rev.iloc[0]
        out["revision_up_30d"] = _safe_int(row.get("upLast30days"))
        out["revision_down_30d"] = _safe_int(row.get("downLast30days"))
    except Exception:
        pass


def _round(x, ndigits: int = 1):
    return round(x, ndigits) if isinstance(x, (int, float)) else x
