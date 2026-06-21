"""Payoff-asymmetry engine (ROADMAP #14a).

The system predicts *whether* a company beats. The money is in *how the stock
reacts*. This module measures, from a ticker's historical earnings prints, the
realised day-1 reaction conditioned on beat vs. miss, then turns it into an
expected value:

    EV = P(beat) · E[move | beat] + (1 − P(beat)) · E[move | miss]

The killer pattern this exposes: high-multiple names that move +4% on a beat but
−12% on a miss have negative expectancy even with a 70%-accurate beat model. The
council/validator (#14b) consume these numbers; this module only computes and
persists them. All network access is guarded — failures yield a neutral dict,
never an exception, so screening never blocks.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _safe_float(val) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Pure computation (no network — unit-tested directly)
# ---------------------------------------------------------------------------

def compute_asymmetry(reactions: list[dict], implied_move_pct: float | None = None) -> dict:
    """Aggregate historical earnings reactions into asymmetry stats.

    `reactions` is a list of {"beat": bool|None, "day1_move_pct": float|None}.

    Returns a dict with: n_prints, n_beats, n_misses, e_move_beat, e_move_miss,
    fade_rate (share of beats that closed red day-1), avg_abs_move, and
    coverage_ratio (avg |move| ÷ implied_move; >1 = habitually exceeds the
    priced-in move).
    """
    moves_beat = [r["day1_move_pct"] for r in reactions
                  if r.get("beat") is True and r.get("day1_move_pct") is not None]
    moves_miss = [r["day1_move_pct"] for r in reactions
                  if r.get("beat") is False and r.get("day1_move_pct") is not None]
    all_moves = [r["day1_move_pct"] for r in reactions if r.get("day1_move_pct") is not None]

    avg_abs_move = _mean([abs(m) for m in all_moves])
    fade_rate = (
        sum(1 for m in moves_beat if m < 0) / len(moves_beat) if moves_beat else None
    )
    coverage_ratio = (
        avg_abs_move / implied_move_pct
        if avg_abs_move is not None and implied_move_pct
        else None
    )

    return {
        "n_prints":       len(all_moves),
        "n_beats":        len(moves_beat),
        "n_misses":       len(moves_miss),
        "e_move_beat":    _round(_mean(moves_beat)),
        "e_move_miss":    _round(_mean(moves_miss)),
        "fade_rate":      _round(fade_rate, 2),
        "avg_abs_move":   _round(avg_abs_move),
        "coverage_ratio": _round(coverage_ratio, 2),
    }


def beat_score_to_p_beat(beat_score: float | None) -> float | None:
    """Map a −5..+5 beat_score to a beat probability.

    Heuristic linear map clamped to [0.05, 0.95]: −5→0.25, 0→0.50, +5→0.75.
    Intentionally conservative; #1 (`suggest-weights`/calibration) can replace
    this with an empirically fitted curve once enough outcomes accumulate.
    """
    if beat_score is None:
        return None
    return max(0.05, min(0.95, 0.5 + 0.05 * float(beat_score)))


def expected_value(e_move_beat, e_move_miss, p_beat) -> float | None:
    """EV of a long into the print, in % of spot. None if inputs are missing."""
    if e_move_beat is None or e_move_miss is None or p_beat is None:
        return None
    return p_beat * e_move_beat + (1 - p_beat) * e_move_miss


def assemble(reactions: list[dict], beat_score=None, implied_move_pct=None) -> dict:
    """Combine reactions + beat_score + implied move into the persisted record."""
    asym = compute_asymmetry(reactions, implied_move_pct)
    p_beat = beat_score_to_p_beat(beat_score)
    ev = expected_value(asym["e_move_beat"], asym["e_move_miss"], p_beat)
    asym["p_beat"] = _round(p_beat, 2)
    asym["ev"] = _round(ev)
    # EV relative to the priced-in move: the playbook's long filter is
    # EV > 0 AND ev_to_implied > 0.25. None when the implied move is unknown.
    asym["ev_to_implied"] = (
        _round(ev / implied_move_pct, 2)
        if ev is not None and implied_move_pct
        else None
    )
    return asym


def _round(x, ndigits: int = 1):
    return round(x, ndigits) if isinstance(x, (int, float)) else x


# ---------------------------------------------------------------------------
# Data fetch (yfinance — guarded)
# ---------------------------------------------------------------------------

def fetch_earnings_reactions(ticker: str, n_prints: int = 8) -> list[dict]:
    """Return up to `n_prints` recent prints with beat flag + day-1 reaction.

    One earnings_dates call + one price-history call per ticker. Each element:
    {"date": "YYYY-MM-DD", "beat": bool|None, "surprise_pct": float|None,
     "day1_move_pct": float|None}. Returns [] on any failure.
    """
    try:
        import yfinance as yf
        from tradingagents.dataflows.stockstats_utils import yf_retry
    except ImportError:
        return []

    def _safe(fn):
        try:
            return yf_retry(fn)
        except Exception:
            return None

    stock = yf.Ticker(ticker)
    edf = _safe(lambda: stock.earnings_dates)
    if edf is None or getattr(edf, "empty", True):
        return []

    today = datetime.now().date()
    rows = []
    try:
        for idx, row in edf.iterrows():
            d = idx.date() if hasattr(idx, "date") else idx
            if d > today:
                continue  # future / unreported
            reported = _safe_float(row.get("Reported EPS"))
            estimate = _safe_float(row.get("EPS Estimate"))
            surprise = _safe_float(row.get("Surprise(%)"))
            if reported is None and surprise is None:
                continue
            if reported is not None and estimate is not None:
                beat = reported >= estimate
            elif surprise is not None:
                beat = surprise >= 0
            else:
                beat = None
            rows.append({"date": d, "beat": beat, "surprise_pct": surprise})
    except Exception:
        return []

    rows.sort(key=lambda r: r["date"], reverse=True)
    rows = rows[:n_prints]
    if not rows:
        return []

    earliest = min(r["date"] for r in rows)
    hist = _safe(lambda: stock.history(
        start=(earliest - timedelta(days=7)).strftime("%Y-%m-%d"),
        end=(today + timedelta(days=1)).strftime("%Y-%m-%d"),
    ))

    reactions = []
    for r in rows:
        reactions.append({
            "date":          r["date"].strftime("%Y-%m-%d"),
            "beat":          r["beat"],
            "surprise_pct":  r["surprise_pct"],
            "day1_move_pct": _day1_move(hist, r["date"]),
        })
    return reactions


def _day1_move(hist, target_date) -> float | None:
    """Pct change from the close before the print to the close just after.

    Mirrors the calibrator: `before` = last close < target, `after` = the second
    trading day on/after target (captures after-hours prints that react T+1).
    """
    if hist is None or getattr(hist, "empty", True):
        return None
    try:
        dates = [d.date() if hasattr(d, "date") else d for d in hist.index]
        closes = list(hist["Close"])
        before = [i for i, d in enumerate(dates) if d < target_date]
        after = [i for i, d in enumerate(dates) if d >= target_date]
        if not before or not after:
            return None
        price_before = _safe_float(closes[before[-1]])
        idx_after = after[1] if len(after) > 1 else after[0]
        price_after = _safe_float(closes[idx_after])
        if not price_before or price_after is None:
            return None
        return round((price_after - price_before) / price_before * 100, 2)
    except Exception:
        return None


def build_asymmetry(ticker: str, beat_score=None, implied_move_pct=None, n_prints: int = 8) -> dict:
    """Fetch reactions and assemble the persisted asymmetry record (guarded)."""
    try:
        reactions = fetch_earnings_reactions(ticker, n_prints=n_prints)
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s: asymmetry fetch failed: %s", ticker, exc)
        reactions = []
    record = assemble(reactions, beat_score=beat_score, implied_move_pct=implied_move_pct)
    record["reactions"] = reactions  # kept for traceability / later backtests
    return record


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def format_asymmetry(asym: dict | None) -> str:
    """One-line advisory summary for the council ticker section."""
    if not asym or not asym.get("n_prints"):
        return "Not available"

    def _na(v, fmt):
        return fmt.format(v) if v is not None else "n/a"

    ev = asym.get("ev")
    ev_ratio = asym.get("ev_to_implied")
    ev_str = _na(ev, "{:+.1f}%")
    if ev is not None and ev_ratio is not None:
        ev_str += f" (EV/move {ev_ratio:+.2f})"
    return (
        f"{asym['n_prints']} prints ({asym['n_beats']}B/{asym['n_misses']}M): "
        f"E[beat]={_na(asym.get('e_move_beat'), '{:+.1f}%')} "
        f"E[miss]={_na(asym.get('e_move_miss'), '{:+.1f}%')} "
        f"fade={_na(asym.get('fade_rate'), '{:.0%}')} | "
        f"coverage={_na(asym.get('coverage_ratio'), '{:.2f}x')} | "
        f"EV(long)={ev_str}"
    )
