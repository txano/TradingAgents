"""Pricing context for the allocation council.

The council sizes binary earnings-event bets, so it needs to know what the
market already expects: spot price, valuation, and — most importantly — the
options-implied earnings move (ATM straddle cost / spot for the first expiry
after the earnings date). Without this, "is it priced in?" is unanswerable.
"""

import logging
from datetime import date, datetime

logger = logging.getLogger(__name__)


def _mid(row) -> float | None:
    """Mid price of an option row; falls back to lastPrice."""
    try:
        bid, ask = float(row.get("bid") or 0), float(row.get("ask") or 0)
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
        last = float(row.get("lastPrice") or 0)
        return last if last > 0 else None
    except (TypeError, ValueError):
        return None


def pick_expiry(expiries: list[str], earnings_date: str | None) -> str | None:
    """First option expiry on/after the earnings date (or the nearest one).

    earnings_date may be messy ("2026-06-15, 2026-06-17", "Not found"); only
    the leading YYYY-MM-DD is used. Falls back to the first expiry when the
    date is missing or unparseable.
    """
    if not expiries:
        return None
    target: date | None = None
    if earnings_date:
        try:
            target = datetime.strptime(str(earnings_date)[:10], "%Y-%m-%d").date()
        except ValueError:
            target = None
    if target is not None:
        for exp in expiries:
            try:
                if datetime.strptime(exp, "%Y-%m-%d").date() >= target:
                    return exp
            except ValueError:
                continue
    return expiries[0]


def compute_implied_move(spot: float, calls, puts) -> float | None:
    """Implied move in % of spot from the ATM straddle (call mid + put mid).

    calls/puts are option-chain DataFrames with strike/bid/ask/lastPrice.
    """
    if not spot or spot <= 0:
        return None
    try:
        call_strikes = {float(s) for s in calls["strike"].tolist()}
        put_strikes = {float(s) for s in puts["strike"].tolist()}
    except Exception:
        return None
    common = call_strikes & put_strikes
    if not common:
        return None
    atm = min(common, key=lambda s: abs(s - spot))

    try:
        call_row = calls[calls["strike"] == atm].iloc[0].to_dict()
        put_row = puts[puts["strike"] == atm].iloc[0].to_dict()
    except Exception:
        return None
    call_mid, put_mid = _mid(call_row), _mid(put_row)
    if call_mid is None or put_mid is None:
        return None
    return round((call_mid + put_mid) / spot * 100, 1)


def fetch_pricing_context(ticker: str, earnings_date: str | None = None) -> dict:
    """Fetch spot, valuation, and implied earnings move from yfinance.

    Every field is individually guarded; missing data stays None so the
    formatted summary degrades to "n/a" per field.
    """
    out = {
        "price": None,
        "market_cap": None,
        "forward_pe": None,
        "week52_position_pct": None,
        "implied_move_pct": None,
        "implied_move_expiry": None,
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

    fast = _safe(lambda: stock.fast_info)
    if fast is not None:
        for src, dst in [("last_price", "price"), ("market_cap", "market_cap")]:
            try:
                value = getattr(fast, src, None) or fast[src]
                if value:
                    out[dst] = float(value)
            except Exception:
                pass
        try:
            low, high = float(fast["year_low"]), float(fast["year_high"])
            if out["price"] and high > low:
                out["week52_position_pct"] = round((out["price"] - low) / (high - low) * 100)
        except Exception:
            pass

    info = _safe(lambda: stock.info) or {}
    fpe = info.get("forwardPE")
    if isinstance(fpe, (int, float)):
        out["forward_pe"] = round(float(fpe), 1)
    if out["price"] is None and isinstance(info.get("currentPrice"), (int, float)):
        out["price"] = float(info["currentPrice"])

    if out["price"]:
        expiries = _safe(lambda: list(stock.options)) or []
        expiry = pick_expiry(expiries, earnings_date)
        if expiry:
            chain = _safe(lambda: stock.option_chain(expiry))
            if chain is not None:
                move = compute_implied_move(out["price"], chain.calls, chain.puts)
                if move is not None:
                    out["implied_move_pct"] = move
                    out["implied_move_expiry"] = expiry

    return out


def format_pricing(pricing: dict | None) -> str:
    """One-line pricing summary for the council's ticker section."""
    if not pricing:
        return "Not available"

    def _na(value, fmt):
        return fmt.format(value) if value is not None else "n/a"

    cap = pricing.get("market_cap")
    cap_str = f"${cap / 1e9:,.1f}B" if cap else "n/a"
    move = pricing.get("implied_move_pct")
    move_str = (
        f"±{move:.1f}% (straddle, exp {pricing.get('implied_move_expiry', '?')})"
        if move is not None
        else "n/a"
    )
    return (
        f"Price: {_na(pricing.get('price'), '${:,.2f}')} | MktCap: {cap_str} | "
        f"Fwd P/E: {_na(pricing.get('forward_pe'), '{:.1f}')} | "
        f"52w range position: {_na(pricing.get('week52_position_pct'), '{:.0f}%')} | "
        f"Implied earnings move: {move_str}"
    )
