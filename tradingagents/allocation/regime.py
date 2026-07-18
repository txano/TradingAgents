"""Tactical market-regime gates (ROADMAP #15b, playbook tactical overlay).

Two cheap, deterministic gates that scale position sizing down in hostile tape:

  * risk-off — SPX below its 50-dma or VIX above a threshold. In risk-off tape,
    beats get sold; halve all earnings position sizes.
  * macro collision — an FOMC decision or CPI release within ±2 sessions of the
    print contaminates the post-earnings reaction; halve that position again
    (or skip).

Distinct from the slow structural governor (#6b Shiller CAPE / Buffett
indicator): this is the fast tactical layer. Both the synthesis prompt and the
deterministic validator read the same regime dict, so the multiplier is
enforced, not merely suggested. All network access is guarded; failures yield
None fields, never an exception.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

VIX_RISK_OFF = 22.0          # VIX close above this → risk-off
MACRO_WINDOW_SESSIONS = 2    # print within ±N sessions of FOMC/CPI → collision
RISK_OFF_MULTIPLIER = 0.5    # sizing multiplier applied in risk-off tape
COLLISION_MULTIPLIER = 0.5   # extra multiplier when the print collides with macro

# Best-effort static calendars — decision day for FOMC, release day for CPI.
# Maintain annually (Fed publishes meeting dates years ahead; BLS publishes the
# CPI schedule each autumn). Past years are kept for trade-log backfill.
FOMC_DATES = (
    # 2025
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    # 2026
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
)
CPI_DATES = (
    # 2025 (Q4 releases were shutdown-disrupted; dates approximate)
    "2025-01-15", "2025-02-12", "2025-03-12", "2025-04-10",
    "2025-05-13", "2025-06-11", "2025-07-15", "2025-08-12",
    "2025-09-11", "2025-10-24", "2025-11-13", "2025-12-18",
    # 2026
    "2026-01-13", "2026-02-11", "2026-03-11", "2026-04-10",
    "2026-05-12", "2026-06-10", "2026-07-14", "2026-08-12",
    "2026-09-11", "2026-10-13", "2026-11-12", "2026-12-10",
)


def _to_date(value) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _business_days_between(a: date, b: date) -> int:
    """Whole business days separating a and b (0 = same day; weekends skipped)."""
    if a > b:
        a, b = b, a
    days = 0
    cur = a
    while cur < b:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            days += 1
    return days


def macro_collisions(earnings_date, window_sessions: int = MACRO_WINDOW_SESSIONS) -> list[str]:
    """FOMC/CPI events within ±window_sessions of the print. Pure date check.

    Returns strings like "FOMC 2026-07-29 (+2 sessions)" — sign is the event's
    position relative to the print. Empty list = clear window (or unknown date).
    """
    ed = _to_date(earnings_date)
    if ed is None:
        return []
    out = []
    for label, dates in (("FOMC", FOMC_DATES), ("CPI", CPI_DATES)):
        for d_str in dates:
            d = _to_date(d_str)
            if d is None or abs((d - ed).days) > 10:  # cheap pre-filter
                continue
            sessions = _business_days_between(ed, d)
            if sessions <= window_sessions:
                sign = "+" if d >= ed else "-"
                out.append(f"{label} {d_str} ({sign}{sessions} sessions)")
    return out


def compute_regime(spx_close, spx_50dma, vix, as_of: str | None = None) -> dict:
    """Pure classification from the three inputs; None inputs stay None."""
    below = (
        spx_close < spx_50dma
        if isinstance(spx_close, (int, float)) and isinstance(spx_50dma, (int, float))
        else None
    )
    elevated = vix > VIX_RISK_OFF if isinstance(vix, (int, float)) else None
    risk_off = None if below is None and elevated is None else bool(below or elevated)
    flags = []
    if below:
        flags.append("SPX < 50-dma")
    if elevated:
        flags.append(f"VIX > {VIX_RISK_OFF:.0f}")
    return {
        "as_of": as_of,
        "spx_close": _round(spx_close),
        "spx_50dma": _round(spx_50dma),
        "spx_below_50dma": below,
        "vix": _round(vix),
        "vix_elevated": elevated,
        "risk_off": risk_off,
        "flags": flags,
    }


def fetch_regime(trade_date: str | None = None) -> dict:
    """Fetch SPX vs 50-dma and VIX from yfinance and classify the regime.

    When trade_date is given, series are truncated to it so re-runs on an old
    folder reproduce the regime as of that date. Failures yield None fields.
    """
    import yfinance as yf

    cutoff = _to_date(trade_date)

    def _closes(symbol: str, period: str):
        try:
            hist = yf.Ticker(symbol).history(period=period)
            if hist is None or hist.empty:
                return None
            if cutoff is not None:
                hist = hist[[d.date() <= cutoff for d in hist.index]]
            return hist["Close"] if len(hist) else None
        except Exception as exc:
            logger.warning("regime: %s fetch failed: %s", symbol, exc)
            return None

    spx = _closes("^GSPC", "6mo")
    spx_close = float(spx.iloc[-1]) if spx is not None else None
    spx_50dma = float(spx.tail(50).mean()) if spx is not None and len(spx) >= 50 else None
    vix_series = _closes("^VIX", "1mo")
    vix = float(vix_series.iloc[-1]) if vix_series is not None else None

    as_of = (cutoff or date.today()).isoformat()
    return compute_regime(spx_close, spx_50dma, vix, as_of=as_of)


def sizing_multiplier(regime: dict | None, collisions: list | None = None) -> float:
    """Combined #15b multiplier: ×0.5 in risk-off, ×0.5 again on a macro collision."""
    mult = 1.0
    if regime and regime.get("risk_off"):
        mult *= RISK_OFF_MULTIPLIER
    if collisions:
        mult *= COLLISION_MULTIPLIER
    return mult


def format_regime(regime: dict | None) -> str:
    """One-line regime summary for the council prompt / report."""
    if not regime or regime.get("risk_off") is None:
        return "Not available (regime data could not be fetched — assume normal sizing)"
    spx, dma, vix = regime.get("spx_close"), regime.get("spx_50dma"), regime.get("vix")
    parts = [
        f"SPX {spx:,.0f} vs 50-dma {dma:,.0f}" if spx is not None and dma is not None else "SPX n/a",
        f"VIX {vix:.1f}" if vix is not None else "VIX n/a",
    ]
    if regime["risk_off"]:
        return (
            f"⚠ RISK-OFF ({'; '.join(regime.get('flags') or [])}) — {' | '.join(parts)} — "
            f"halve all position sizes"
        )
    return f"Normal ({' | '.join(parts)}) — full sizing permitted"


def format_collisions(collisions: list[str]) -> str:
    """Per-ticker macro line for the council ticker section."""
    if not collisions:
        return "No FOMC/CPI within ±2 sessions of the print"
    return f"⚠ COLLISION: {'; '.join(collisions)} — reaction will be contaminated; halve or skip"


def _round(x, ndigits: int = 2):
    return round(float(x), ndigits) if isinstance(x, (int, float)) else None
