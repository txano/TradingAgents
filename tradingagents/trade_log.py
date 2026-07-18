"""Trade-log schema v2 + enrichment (ROADMAP #17 — log schema v2).

The trade log (`reports/trades.json`) started as a thin record of fills. To make
the #17 backtest harness possible, every trade needs the *context it was entered
in* (T-1), the *outcome* of the print, the *reaction*, and how it was *managed*.

This module is deliberately cheap: it defines the canonical v2 field set, an
idempotent `ensure_v2()` that adds the missing keys (null) to any trade dict, and
enrichment helpers that backfill what we can *today* from already-saved screening
artifacts (`pricing.json` / `crowding.json` / `asymmetry.json`) plus a guarded
yfinance call for the realised outcome/reaction.

Fields we can't fill yet are kept as explicit nulls so data accumulates with a
stable shape; each is owned by a later roadmap item:
  * iv_rank / term_ratio / skew_25d   → #3b (IBKR options pipeline)
  * gate_path / action                → #16 (trade-management triage)
(regime_flag is filled from the run's regime.json when present — #15.)

All network access is guarded — failures leave fields None, never raise, so a
backfill pass can never corrupt the log.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

# --- T-1 context: what the market looked like going into the print ----------
T1_CONTEXT_FIELDS = (
    "implied_move_pct",        # pricing.json  — options-implied earnings move
    "iv_rank",                 # #3b (IBKR) — null for now
    "term_ratio",              # #3b (IBKR) — null for now
    "skew_25d",                # #3b (IBKR) — null for now
    "runup_1m_pct",            # crowding.json — 1m pre-print return
    "runup_vs_sector_1m",      # crowding.json — 1m return vs sector ETF
    "dist_52w_high_pct",       # crowding.json — distance below 52w high
    "revision_direction_30d",  # crowding.json — sign(up - down) EPS revisions (30d)
    "short_interest_pct",      # yfinance info — shortPercentOfFloat
    "regime_flag",             # regime.json (run root) — "risk_off"/"normal" (#15)
)

# --- Outcome: what actually happened in the report --------------------------
OUTCOME_FIELDS = (
    "beat_eps",                # bool — reported EPS >= estimate
    "beat_rev",                # bool — reported revenue >= estimate (best-effort)
    "guide",                   # "up"/"down"/"inline" — not auto-derivable yet (null)
)

# --- Reaction: how the stock moved ------------------------------------------
REACTION_FIELDS = (
    "move_d1",                 # % close-before → close-after the print
    "move_d5",                 # % close-before → +5 trading days
    "move_d20",                # % close-before → +20 trading days
    "coverage_ratio",          # asymmetry.json — avg |move| / implied move
)

# --- Management: how the position was handled (#16) --------------------------
MGMT_FIELDS = (
    "gate_path",               # #16 — null for now
    "action",                  # #16 — null for now
    "pnl_final",               # final realised P&L (mirrors `pnl` when closed)
)

V2_FIELDS = T1_CONTEXT_FIELDS + OUTCOME_FIELDS + REACTION_FIELDS + MGMT_FIELDS


def _safe_float(val):
    if val is None:
        return None
    try:
        f = float(val)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def ensure_v2(trade: dict) -> dict:
    """Add any missing v2 fields (as None) to a trade dict in place; idempotent.

    Sets `schema_version` to 2. Existing values are never overwritten, so this is
    safe to call on already-enriched trades.
    """
    for key in V2_FIELDS:
        trade.setdefault(key, None)
    # pnl_final mirrors the realised pnl for closed trades unless already set.
    if trade.get("pnl_final") is None and trade.get("pnl") is not None:
        trade["pnl_final"] = trade["pnl"]
    trade["schema_version"] = SCHEMA_VERSION
    return trade


# ---------------------------------------------------------------------------
# Linking a trade → the screening run it came from
# ---------------------------------------------------------------------------

# Batch-screen run dirs are named `screening_YYYY-MM-DD_...` (legacy) or
# `earnings_YYYY-MM-DD_...` (current `reports/earnings/` layout). Both hold one
# subdir per ticker plus a screening_table.md.
_RUN_PREFIXES = ("screening_", "earnings_")


def _run_date(name: str) -> str | None:
    """Extract a YYYY-MM-DD date from a `{screening,earnings}_YYYY-MM-DD_...` name."""
    parts = name.split("_")
    for p in parts[1:3]:
        try:
            datetime.strptime(p, "%Y-%m-%d")
            return p
        except ValueError:
            continue
    return None


def find_screening_run(ticker: str, ref_date: str | None, reports_dir: Path) -> Path | None:
    """Return the ticker dir of the most recent screening run on/before ref_date.

    Searches both the current (`reports/earnings/earnings_*`) and legacy
    (`reports/[earnings/]screening_*`) batch-run layouts for a `{run}/{ticker}/`
    folder whose run date is ≤ ref_date. Picks the newest qualifying run (the
    prediction the trade most plausibly acted on). Returns None when nothing matches.
    """
    reports_dir = Path(reports_dir)
    if not reports_dir.is_dir():
        return None

    candidates: list[tuple[str, Path]] = []
    for run_dir in reports_dir.glob("**/*_*"):
        if not run_dir.is_dir() or not run_dir.name.startswith(_RUN_PREFIXES):
            continue
        rdate = _run_date(run_dir.name)
        if rdate is None:
            continue
        if ref_date and rdate > ref_date[:10]:
            continue
        ticker_dir = run_dir / ticker
        if ticker_dir.is_dir():
            candidates.append((rdate, ticker_dir))

    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    return candidates[-1][1]


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Enrichment from saved screening artifacts (no network)
# ---------------------------------------------------------------------------

def enrich_from_artifacts(trade: dict, reports_dir: Path = Path("reports")) -> dict:
    """Fill T-1 context + coverage_ratio from the linked run's saved JSON.

    Locates the screening run via `find_screening_run`, sets `screening_run`, and
    pulls fields out of pricing/crowding/asymmetry.json. Only fills fields that are
    currently None, so manual values and prior enrichment are preserved.
    """
    ensure_v2(trade)
    ticker = trade.get("ticker")
    if not ticker:
        return trade
    ref = trade.get("trade_date") or trade.get("exit_date")
    ticker_dir = find_screening_run(ticker, ref, reports_dir)
    if ticker_dir is None:
        return trade

    if not trade.get("screening_run"):
        trade["screening_run"] = ticker_dir.parent.name

    def _set(key, value):
        if value is not None and trade.get(key) is None:
            trade[key] = value

    pricing = _load_json(ticker_dir / "pricing.json") or {}
    _set("implied_move_pct", _safe_float(pricing.get("implied_move_pct")))

    crowding = _load_json(ticker_dir / "crowding.json") or {}
    _set("runup_1m_pct", _safe_float(crowding.get("runup_1m_pct")))
    _set("runup_vs_sector_1m", _safe_float(crowding.get("runup_1m_vs_sector")))
    _set("dist_52w_high_pct", _safe_float(crowding.get("dist_52w_high_pct")))
    up = crowding.get("revision_up_30d")
    down = crowding.get("revision_down_30d")
    if trade.get("revision_direction_30d") is None and (up is not None or down is not None):
        net = (up or 0) - (down or 0)
        trade["revision_direction_30d"] = "up" if net > 0 else ("down" if net < 0 else "flat")

    asym = _load_json(ticker_dir / "asymmetry.json") or {}
    _set("coverage_ratio", _safe_float(asym.get("coverage_ratio")))

    # regime.json lives at the run root (global per run), not in the ticker dir (#15)
    regime = _load_json(ticker_dir.parent / "regime.json") or {}
    if trade.get("regime_flag") is None and regime.get("risk_off") is not None:
        trade["regime_flag"] = "risk_off" if regime["risk_off"] else "normal"

    return trade


# ---------------------------------------------------------------------------
# Enrichment from yfinance (guarded network)
# ---------------------------------------------------------------------------

def _earnings_date_for(trade: dict, ticker_dir: Path | None) -> str | None:
    """Best-effort earnings date: saved score JSON first, then the trade itself."""
    if ticker_dir is not None:
        raw = _load_json(ticker_dir / "earnings_raw_data.json") or {}
        ed = raw.get("earnings_date")
        if isinstance(ed, str) and len(ed) >= 10:
            try:
                datetime.strptime(ed[:10], "%Y-%m-%d")
                return ed[:10]
            except ValueError:
                pass
    return None


def _moves_after(hist, target_date) -> dict:
    """Return {move_d1, move_d5, move_d20} %: close-before → close at +1/+5/+20."""
    out = {"move_d1": None, "move_d5": None, "move_d20": None}
    if hist is None or getattr(hist, "empty", True):
        return out
    try:
        dates = [d.date() if hasattr(d, "date") else d for d in hist.index]
        closes = [_safe_float(c) for c in hist["Close"]]
        before = [i for i, d in enumerate(dates) if d < target_date]
        after = [i for i, d in enumerate(dates) if d >= target_date]
        if not before or not after:
            return out
        price_before = closes[before[-1]]
        if not price_before:
            return out
        # after[0] is the day the print is reflected; the d1 close is after[1]
        # to capture after-hours prints that move T+1 (mirrors the calibrator).
        base = 1 if len(after) > 1 else 0
        for key, horizon in (("move_d1", 0), ("move_d5", 4), ("move_d20", 19)):
            idx = base + horizon
            if idx < len(after):
                p = closes[after[idx]]
                if p is not None:
                    out[key] = round((p - price_before) / price_before * 100, 2)
    except Exception:
        pass
    return out


def enrich_outcome(trade: dict, reports_dir: Path = Path("reports")) -> dict:
    """Fill outcome (beat_eps), reaction (move_d1/d5/d20) and short_interest_pct.

    Needs an earnings date (from the linked run's raw data) and yfinance. All
    network access is guarded; on any failure the fields stay None.
    """
    ensure_v2(trade)
    ticker = trade.get("ticker")
    if not ticker:
        return trade
    try:
        import yfinance as yf
        from tradingagents.dataflows.stockstats_utils import yf_retry
    except ImportError:
        return trade

    def _safe(fn):
        try:
            return yf_retry(fn)
        except Exception:
            return None

    ref = trade.get("trade_date") or trade.get("exit_date")
    ticker_dir = find_screening_run(ticker, ref, reports_dir)
    earnings_date = _earnings_date_for(trade, ticker_dir)

    stock = yf.Ticker(ticker)

    if trade.get("short_interest_pct") is None:
        info = _safe(lambda: stock.info) or {}
        spf = info.get("shortPercentOfFloat")
        if isinstance(spf, (int, float)):
            trade["short_interest_pct"] = round(float(spf) * 100, 2)

    if earnings_date is None:
        return trade
    target = datetime.strptime(earnings_date, "%Y-%m-%d").date()

    if trade.get("beat_eps") is None:
        edf = _safe(lambda: stock.earnings_dates)
        if edf is not None and not getattr(edf, "empty", True):
            try:
                for idx, row in edf.iterrows():
                    d = idx.date() if hasattr(idx, "date") else idx
                    if abs((d - target).days) <= 7:
                        rep = _safe_float(row.get("Reported EPS"))
                        est = _safe_float(row.get("EPS Estimate"))
                        surp = _safe_float(row.get("Surprise(%)"))
                        if rep is not None and est is not None:
                            trade["beat_eps"] = rep >= est
                        elif surp is not None:
                            trade["beat_eps"] = surp >= 0
                        break
            except Exception:
                pass

    if any(trade.get(k) is None for k in ("move_d1", "move_d5", "move_d20")):
        hist = _safe(lambda: stock.history(
            start=(target - timedelta(days=7)).strftime("%Y-%m-%d"),
            end=(target + timedelta(days=40)).strftime("%Y-%m-%d"),
        ))
        moves = _moves_after(hist, target)
        for k, v in moves.items():
            if trade.get(k) is None:
                trade[k] = v

    return trade


# ---------------------------------------------------------------------------
# Backfill driver
# ---------------------------------------------------------------------------

def backfill(
    trades: list[dict],
    reports_dir: Path = Path("reports"),
    with_network: bool = True,
    tickers: set[str] | None = None,
) -> dict:
    """Enrich a list of trade dicts in place; return a small summary.

    `with_network=False` skips the yfinance outcome/reaction pass (artifacts only).
    `tickers` optionally restricts enrichment to a subset of symbols.
    """
    reports_dir = Path(reports_dir)
    linked = filled_outcome = 0
    for t in trades:
        if tickers and t.get("ticker") not in tickers:
            ensure_v2(t)
            continue
        before_run = t.get("screening_run")
        enrich_from_artifacts(t, reports_dir)
        if t.get("screening_run") and not before_run:
            linked += 1
        if with_network:
            had_move = t.get("move_d1") is not None
            enrich_outcome(t, reports_dir)
            if t.get("move_d1") is not None and not had_move:
                filled_outcome += 1
    return {
        "total": len(trades),
        "newly_linked": linked,
        "newly_filled_outcome": filled_outcome,
    }


# ---------------------------------------------------------------------------
# Risk-adjusted statistics (#17/#18 — Sharpe / Sortino / max drawdown)
# ---------------------------------------------------------------------------

def risk_stats(trades: list[dict]) -> dict:
    """Per-trade risk-adjusted stats from closed-trade returns.

    Returns are per-trade percentages (pnl_pct, reconstructed from
    pnl/shares/entry_price when missing), equal-weighted and not annualised —
    with irregular event-driven holding periods, per-trade is the honest unit.

    Sharpe  = mean(r) / std(r)                     (population std)
    Sortino = mean(r) / downside_dev(r)            (target 0; None when no losers)
    Max drawdown = largest peak-to-trough drop on the cumulative sum of
    per-trade returns in exit-date order, in percentage points (assumes
    equal sizing per trade).
    """
    dated: list[tuple[str, float]] = []
    for t in trades:
        r = _safe_float(t.get("pnl_pct"))
        if r is None:
            pnl, sh, ep = _safe_float(t.get("pnl")), _safe_float(t.get("shares")), _safe_float(t.get("entry_price"))
            if pnl is not None and sh and ep:
                r = pnl / (sh * ep) * 100
        if r is not None:
            dated.append((str(t.get("exit_date") or t.get("logged_at") or ""), r))

    out = {"n": len(dated), "sharpe": None, "sortino": None,
           "max_drawdown_pp": None, "no_losses": False}
    if len(dated) < 2:
        return out

    rets = [r for _, r in dated]
    n = len(rets)
    mean = sum(rets) / n
    std = math.sqrt(sum((x - mean) ** 2 for x in rets) / n)
    downside = math.sqrt(sum(min(x, 0.0) ** 2 for x in rets) / n)

    out["sharpe"] = round(mean / std, 2) if std > 0 else None
    if downside > 0:
        out["sortino"] = round(mean / downside, 2)
    else:
        out["no_losses"] = True  # Sortino undefined (infinite) — flag instead

    cum = peak = max_dd = 0.0
    for _, r in sorted(dated, key=lambda d: d[0]):
        cum += r
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    out["max_drawdown_pp"] = round(max_dd, 1)
    return out
