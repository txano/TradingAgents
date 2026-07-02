"""Insider-signal engine (ROADMAP #18).

Raw insider activity is noisy — routine 10b5-1 sales, option exercises, and gifts
dominate the tape. The signal that historically produces strong forward returns is
narrow: **cluster buys** (several distinct insiders buying in a short window) and
**sell→buy reversals** (an insider who was selling and starts buying). This module
detects those patterns deterministically from the yfinance insider tape, rather
than leaving the LLM to spot them in a CSV.

Insider buying is a bullish *support* signal (insiders rarely buy except when
bullish); insider selling is weak/neutral (often programmatic) and is discounted.
All network access is guarded; failures yield a neutral dict, never an exception.

Source note: yfinance is derived from SEC Form-4 but can be incomplete/laggy;
Form-4 (EDGAR) direct is a future upgrade (see #18).
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Tunable thresholds — recalibrate via #17.
LOOKBACK_DAYS = 180          # how far back to pull the tape
RECENT_DAYS = 90             # the "recent" window that carries the signal
CLUSTER_MIN_BUYERS = 3       # distinct net-buying insiders in RECENT = cluster
NOTABLE_BUY_USD = 1_000_000  # a single buy at/above this is called out
HEAVY_SELL_USD = 5_000_000   # net selling at/above this (with no buys) = caution


def _safe_float(val):
    if val is None:
        return None
    try:
        f = float(val)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _classify(text: str) -> str:
    """Map a yfinance insider 'Text' field to buy / sell / other."""
    t = (text or "").lower()
    if "purchase" in t or "buy" in t:
        return "buy"
    if "sale" in t or "sold" in t or "sell" in t:
        return "sell"
    return "other"  # gift, grant, exercise, conversion, disposition — noise


# ---------------------------------------------------------------------------
# Pure computation (unit-tested directly)
# ---------------------------------------------------------------------------

def compute_insider_signal(transactions: list[dict], asof: str | None = None) -> dict:
    """Aggregate normalized insider transactions into a pattern signal.

    `transactions` items: {"date": "YYYY-MM-DD", "insider": str,
    "kind": "buy"|"sell"|"other", "value": float}. `asof` defaults to the most
    recent transaction date (else today).
    """
    dated = []
    for t in transactions:
        d = _parse_date(t.get("date"))
        if d is None:
            continue
        dated.append((d, t))
    if not dated:
        return _empty()

    asof_d = _parse_date(asof) or max(d for d, _ in dated)
    recent_cut = asof_d - timedelta(days=RECENT_DAYS)

    # Per-insider buy/sell value in the recent and older windows.
    recent: dict[str, dict] = {}
    older: dict[str, dict] = {}
    n_buys = n_sells = 0
    buy_value = sell_value = 0.0
    notable_buys: list[dict] = []

    for d, t in dated:
        kind = t.get("kind")
        if kind not in ("buy", "sell"):
            continue
        name = str(t.get("insider") or "?").strip()
        val = _safe_float(t.get("value")) or 0.0
        bucket = recent if d >= recent_cut else older
        agg = bucket.setdefault(name, {"buy": 0.0, "sell": 0.0})
        agg[kind] += val
        if d >= recent_cut:
            if kind == "buy":
                n_buys += 1
                buy_value += val
                if val >= NOTABLE_BUY_USD:
                    notable_buys.append({"insider": name, "value": round(val)})
            else:
                n_sells += 1
                sell_value += val

    net_buyers = [n for n, a in recent.items() if a["buy"] > a["sell"] and a["buy"] > 0]
    net_sellers = [n for n, a in recent.items() if a["sell"] > a["buy"] and a["sell"] > 0]
    cluster_buyers = len(net_buyers)
    cluster_buy = cluster_buyers >= CLUSTER_MIN_BUYERS

    # Reversal: sold in the older window, bought in the recent window.
    reversal_insiders = [
        n for n in recent
        if recent[n]["buy"] > 0 and older.get(n, {}).get("sell", 0) > 0
    ]
    reversal = len(reversal_insiders) > 0

    signal = _label(cluster_buy, reversal, buy_value, sell_value, n_buys)
    flags = _flags(cluster_buy, cluster_buyers, reversal, reversal_insiders,
                   notable_buys, buy_value, sell_value, n_buys)

    return {
        "asof": asof_d.strftime("%Y-%m-%d"),
        "recent_days": RECENT_DAYS,
        "n_buys": n_buys,
        "n_sells": n_sells,
        "n_buy_insiders": len(net_buyers),
        "n_sell_insiders": len(net_sellers),
        "buy_value": round(buy_value),
        "sell_value": round(sell_value),
        "net_value": round(buy_value - sell_value),
        "cluster_buy": cluster_buy,
        "cluster_buyers": cluster_buyers,
        "reversal": reversal,
        "reversal_insiders": reversal_insiders[:4],
        "notable_buys": notable_buys[:4],
        "signal": signal,
        "flags": flags,
    }


def _label(cluster_buy, reversal, buy_value, sell_value, n_buys) -> str:
    if cluster_buy:
        return "cluster buy"
    if reversal:
        return "reversal"
    if n_buys > 0 and buy_value >= sell_value:
        return "net buying"
    if sell_value > buy_value and sell_value >= HEAVY_SELL_USD:
        return "net selling"
    return "quiet"


def _flags(cluster_buy, cluster_buyers, reversal, reversal_insiders,
           notable_buys, buy_value, sell_value, n_buys) -> list[str]:
    flags: list[str] = []
    if cluster_buy:
        flags.append(f"cluster buy: {cluster_buyers} insiders bought (last {RECENT_DAYS}d)")
    if reversal:
        who = ", ".join(_short(n) for n in reversal_insiders[:2])
        flags.append(f"reversal: {who} sold→bought")
    for nb in notable_buys[:2]:
        flags.append(f"${nb['value'] / 1e6:.1f}M buy by {_short(nb['insider'])}")
    if n_buys == 0 and sell_value >= HEAVY_SELL_USD:
        flags.append(f"heavy net selling ${sell_value / 1e6:.1f}M (often routine)")
    return flags


def _short(name: str) -> str:
    """Trim an insider name for compact display."""
    return name.title()[:22]


def _empty() -> dict:
    return {
        "asof": None, "recent_days": RECENT_DAYS,
        "n_buys": 0, "n_sells": 0, "n_buy_insiders": 0, "n_sell_insiders": 0,
        "buy_value": 0, "sell_value": 0, "net_value": 0,
        "cluster_buy": False, "cluster_buyers": 0,
        "reversal": False, "reversal_insiders": [], "notable_buys": [],
        "signal": "quiet", "flags": [],
    }


def _parse_date(val):
    if val is None:
        return None
    if hasattr(val, "date"):
        try:
            return val.date()
        except Exception:
            pass
    try:
        return datetime.strptime(str(val)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Data fetch (yfinance — guarded)
# ---------------------------------------------------------------------------

def fetch_insider_transactions(ticker: str, lookback_days: int = LOOKBACK_DAYS) -> list[dict]:
    """Normalized recent insider transactions for a ticker, newest first. [] on failure."""
    try:
        import yfinance as yf
        from tradingagents.dataflows.stockstats_utils import yf_retry
    except ImportError:
        return []

    try:
        df = yf_retry(lambda: yf.Ticker(ticker).insider_transactions)
    except Exception:
        return []
    if df is None or getattr(df, "empty", True):
        return []

    cutoff = datetime.now().date() - timedelta(days=lookback_days)
    out: list[dict] = []
    try:
        for _, row in df.iterrows():
            d = _parse_date(row.get("Start Date"))
            if d is None or d < cutoff:
                continue
            out.append({
                "date":    d.strftime("%Y-%m-%d"),
                "insider": str(row.get("Insider") or "?"),
                "kind":    _classify(row.get("Text")),
                "value":   _safe_float(row.get("Value")) or 0.0,
                "shares":  _safe_float(row.get("Shares")) or 0.0,
            })
    except Exception:
        return []
    out.sort(key=lambda r: r["date"], reverse=True)
    return out


def build_insider(ticker: str, lookback_days: int = LOOKBACK_DAYS) -> dict:
    """Fetch + compute the insider signal for a ticker (guarded)."""
    try:
        txns = fetch_insider_transactions(ticker, lookback_days=lookback_days)
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s: insider fetch failed: %s", ticker, exc)
        txns = []
    return compute_insider_signal(txns)


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def format_insider(sig: dict | None) -> str:
    """One-line summary for the council ticker section."""
    if not sig or sig.get("asof") is None:
        return "Not available"
    base = (
        f"{sig['signal']} | recent {sig['recent_days']}d: "
        f"{sig['n_buy_insiders']} buyers / {sig['n_sell_insiders']} sellers, "
        f"net ${sig['net_value'] / 1e6:+.1f}M"
    )
    return base + (f" | {', '.join(sig['flags'])}" if sig.get("flags") else "")
