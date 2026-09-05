"""Conditional "wait & decide" entries (ROADMAP #19).

The council can mark a quality name WATCH instead of forcing BUY-or-SKIP: skip
the print, and if the stock sells off to a pre-committed trigger level within a
short post-earnings window, it becomes a buy-the-dip candidate with the same
thesis. This module owns the artifact (`watchlist.json` at the run root) and the
status lifecycle the dashboard renders:

    PENDING   → earnings haven't happened yet; nothing to do
    ARMED     → print is past, inside the watch window; watch price vs trigger
    TRIGGERED → traded at/below the trigger inside the window; actionable now
    EXPIRED   → window closed without a trigger

Flag-only by design (2026-07-18 decision): the system surfaces trigger levels;
the user places orders manually. Because orders are manual, the dashboard needs
somewhere to record what the user did about an entry — see the *user state*
overlay below. All network access is guarded — failures degrade to date-only
status, never an exception.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timezone
from pathlib import Path

from tradingagents.allocation.regime import _business_days_between, _to_date

logger = logging.getLogger(__name__)

# Tunable priors — #17's backtest harness should calibrate these.
WATCH_MIN_FUNDAMENTALS = 2        # WATCH is for quality names only
WATCH_MIN_TRIGGER_FRACTION = 0.5  # trigger must sit ≥ this × implied_move below spot
WATCH_DEFAULT_EXPIRY_SESSIONS = 5
WATCH_MAX_EXPIRY_SESSIONS = 5

WATCHLIST_FILE = "watchlist.json"

# User state lives in reports/ (not the run dir) so run artifacts stay exactly as
# the council produced them, the overlay survives re-runs, and Syncthing carries
# it Mac ↔ Pi alongside trades.json.
WATCHLIST_STATE_FILE = "watchlist_state.json"
USER_STATES = ("purchased", "dismissed")

QUOTE_TTL_SECONDS = 60  # the dashboard polls; don't hammer the quote endpoint
BARS_TTL_SECONDS = 300  # daily bars move slower than the quote that rides on top


def build_watchlist(alloc: dict, contexts: list[dict], trade_date: str | None = None) -> list[dict]:
    """Extract WATCH rows from a parsed allocation into persistable entries.

    Pure: reads the allocation dict + ticker contexts (for earnings date and the
    pre-print reference price) and returns a list of entry dicts.
    """
    if not alloc or not isinstance(alloc.get("allocations"), list):
        return []
    ctx_by_ticker = {c.get("ticker"): c for c in contexts}
    entries = []
    for r in alloc["allocations"]:
        if str(r.get("direction", "")).upper() != "WATCH":
            continue
        ticker = str(r.get("ticker", "")).strip()
        if not ticker:
            continue
        ctx = ctx_by_ticker.get(ticker, {}) or {}
        expiry = r.get("watch_expiry_sessions")
        if not isinstance(expiry, (int, float)) or not 1 <= expiry <= WATCH_MAX_EXPIRY_SESSIONS:
            expiry = WATCH_DEFAULT_EXPIRY_SESSIONS
        entries.append({
            "ticker": ticker,
            "trigger_price": _num(r.get("trigger_price")),
            "watch_amount": _num(r.get("watch_amount")),
            "expiry_sessions": int(expiry),
            "thesis": r.get("rationale") or ctx.get("one_liner") or "",
            "earnings_date": ctx.get("earnings_date"),
            "spot_at_screen": _num(ctx.get("spot_price")),
            "implied_move_pct": _num(ctx.get("implied_move_pct")),
            "created": trade_date or date.today().isoformat(),
        })
    return entries


def save_watchlist(entries: list[dict], run_dir: str | Path) -> None:
    """Write watchlist.json at the run root (only when there are entries)."""
    if not entries:
        return
    path = Path(run_dir) / WATCHLIST_FILE
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def load_watchlist(run_dir: str | Path) -> list[dict]:
    path = Path(run_dir) / WATCHLIST_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# User state overlay — "I bought this" / "drop this alert"
# --------------------------------------------------------------------------- #

def entry_key(run_id: str, ticker: str) -> str:
    """Stable identity for one watch entry: the run it came from + the ticker."""
    return f"{run_id}|{str(ticker).strip().upper()}"


def state_path(reports_root: str | Path | None = None) -> Path:
    return Path(reports_root or "reports") / WATCHLIST_STATE_FILE


def load_states(reports_root: str | Path | None = None) -> dict:
    try:
        data = json.loads(state_path(reports_root).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def set_entry_state(
    run_id: str,
    ticker: str,
    state: str | None,
    reports_root: str | Path | None = None,
    note: str | None = None,
    price: float | None = None,
) -> dict:
    """Mark one entry purchased/dismissed, or clear the mark (``state=None``).

    Returns the stored record ({} once cleared). Unknown states raise ValueError
    so a typo can't silently create a state the dashboard won't render.
    """
    if state is not None and state not in USER_STATES:
        raise ValueError(f"unknown watchlist state {state!r} (expected one of {USER_STATES} or None)")

    path = state_path(reports_root)
    states = load_states(reports_root)
    key = entry_key(run_id, ticker)

    if state is None:
        states.pop(key, None)
        record: dict = {}
    else:
        record = {
            "state": state,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "run_id": run_id,
            "ticker": str(ticker).strip().upper(),
        }
        if note:
            record["note"] = note
        if price is not None:
            record["price"] = round(float(price), 2)
        states[key] = record

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(states, indent=2, sort_keys=True), encoding="utf-8")
    return record


# --------------------------------------------------------------------------- #
# Live quotes
# --------------------------------------------------------------------------- #

_QUOTE_CACHE: dict[str, tuple[float, float]] = {}  # TICKER -> (fetched_at, price)


def fetch_live_price(ticker: str, ttl: float = QUOTE_TTL_SECONDS) -> float | None:
    """Last traded price, or None if unavailable. Guarded and TTL-cached.

    The status lifecycle keys off daily bars (an intraday *low* is what proves a
    trigger was hit); this is purely the "what is it worth right now" number the
    dashboard shows, and doubles as an intraday TRIGGERED upgrade in
    :func:`refresh_entry`.
    """
    key = str(ticker).strip().upper()
    if not key:
        return None
    hit = _QUOTE_CACHE.get(key)
    if hit and (time.time() - hit[0]) < ttl:
        return hit[1]

    try:
        from tradingagents.dataflows.stockstats_utils import yf_retry
    except Exception:
        def yf_retry(fn):  # no retry helper available — call straight through
            return fn()

    price = None
    try:
        import yfinance as yf

        stock = yf.Ticker(key)
        try:
            fast = yf_retry(lambda: stock.fast_info)
            price = _num(getattr(fast, "last_price", None) if fast is not None else None)
            if price is None and fast is not None:
                price = _num(fast["last_price"])
        except Exception:
            price = None
        if price is None:
            hist = yf_retry(lambda: stock.history(period="1d"))
            if hist is not None and not hist.empty:
                price = _num(hist["Close"].iloc[-1])
    except Exception as exc:
        logger.warning("watchlist: %s live quote failed: %s", key, exc)
        return None

    if price is not None and price > 0:
        _QUOTE_CACHE[key] = (time.time(), price)
        return price
    return None


_BARS_CACHE: dict[tuple[str, str], tuple[float, list[dict]]] = {}  # (TICKER, start) -> (at, bars)


def fetch_bars(ticker: str, start: str, ttl: float = BARS_TTL_SECONDS) -> list[dict] | None:
    """Post-print daily bars (oldest-first) for the trigger scan. TTL-cached.

    Separate cache from :func:`fetch_live_price` because it answers a different
    question — an intraday *low* is what proves a trigger was hit, so this can
    lag the quote by minutes without changing a status. Uncached it was the
    dominant cost of the whole watchlist refresh: one sequential yfinance
    history call per entry, ~150 ms each.
    """
    key = str(ticker).strip().upper()
    if not key or not start:
        return None
    hit = _BARS_CACHE.get((key, start))
    if hit and (time.time() - hit[0]) < ttl:
        return hit[1]
    if len(_BARS_CACHE) > 256:   # keyed by (ticker, start): bounded, but not by itself
        now = time.time()
        for k in [k for k, v in _BARS_CACHE.items() if now - v[0] >= ttl]:
            _BARS_CACHE.pop(k, None)

    try:
        import yfinance as yf

        hist = yf.Ticker(key).history(start=start)
        if hist is None or hist.empty:
            return None
        bars = [
            {"date": idx.date().isoformat(), "low": float(row["Low"]), "close": float(row["Close"])}
            for idx, row in hist.iterrows()
        ]
    except Exception as exc:
        logger.warning("watchlist: %s price fetch failed: %s", key, exc)
        return None

    _BARS_CACHE[(key, start)] = (time.time(), bars)
    return bars


def clear_caches() -> None:
    """Drop both price caches. For tests and for forcing a genuine re-fetch."""
    _QUOTE_CACHE.clear()
    _BARS_CACHE.clear()


def entry_status(entry: dict, today: date | None = None, bars: list[dict] | None = None) -> dict:
    """Pure status classification for one watchlist entry.

    Args:
        entry: watchlist entry (needs earnings_date, trigger_price, expiry_sessions).
        today: evaluation date (defaults to date.today()).
        bars: post-print daily bars, oldest-first, each {"date", "low", "close"}.
            Bars from the earnings date onward — for AMC prints the day-of bar is
            pre-print, but a trigger ≥ half the implied move below spot is far
            below normal pre-print lows, so including it is safely conservative.
            None = price history unavailable → date-only status (hit unknowable).

    Returns a dict with status, sessions_elapsed/left, current_price,
    distance_pct (current vs trigger, + = above), hit_date.
    """
    today = today or date.today()
    ed = _to_date(entry.get("earnings_date"))
    trigger = _num(entry.get("trigger_price"))
    expiry = int(entry.get("expiry_sessions") or WATCH_DEFAULT_EXPIRY_SESSIONS)

    out = {"status": "PENDING", "sessions_elapsed": 0, "sessions_left": expiry,
           "current_price": None, "distance_pct": None, "hit_date": None}
    if ed is None or trigger is None or trigger <= 0:
        out["status"] = "UNKNOWN"
        return out
    if today <= ed:
        return out  # PENDING

    window = (bars or [])[:expiry]
    if bars is not None:
        elapsed = len(window)
        last_close = _num(window[-1].get("close")) if window else None
    else:
        elapsed = min(_business_days_between(ed, today), expiry)
        last_close = None

    out["sessions_elapsed"] = elapsed
    out["sessions_left"] = max(expiry - elapsed, 0)
    out["current_price"] = round(last_close, 2) if last_close is not None else None
    if last_close is not None:
        out["distance_pct"] = round((last_close / trigger - 1) * 100, 1)

    for bar in window:
        low = _num(bar.get("low"))
        if low is not None and low <= trigger:
            out["status"] = "TRIGGERED"
            out["hit_date"] = bar.get("date")
            return out

    out["status"] = "EXPIRED" if out["sessions_left"] == 0 else "ARMED"
    return out


def refresh_entry(entry: dict, today: date | None = None, live: bool = True) -> dict:
    """entry + live status, fetching post-print bars from yfinance (guarded).

    With ``live``, the last traded price replaces the daily close as
    ``current_price`` (``price_source`` says which you got) and an ARMED entry
    quoting at/below its trigger is upgraded to TRIGGERED intraday, rather than
    waiting for the daily bar to settle.
    """
    today = today or date.today()
    ed = _to_date(entry.get("earnings_date"))
    bars = None
    if ed is not None and today > ed:
        bars = fetch_bars(entry.get("ticker", ""), ed.isoformat())

    out = {**entry, **entry_status(entry, today=today, bars=bars)}
    out["price_source"] = "close" if out.get("current_price") is not None else None
    out["live_price"] = None
    out["quote_time"] = None
    if not live:
        return out

    price = fetch_live_price(entry.get("ticker", ""))
    if price is None:
        return out

    trigger = _num(entry.get("trigger_price"))
    out["live_price"] = round(price, 2)
    out["quote_time"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out["current_price"] = round(price, 2)
    out["price_source"] = "live"
    if trigger and trigger > 0:
        out["distance_pct"] = round((price / trigger - 1) * 100, 1)
        if out["status"] == "ARMED" and price <= trigger:
            out["status"] = "TRIGGERED"
            out["hit_date"] = today.isoformat()
    return out


def collect_watchlists(
    reports_root: str | Path | None = None,
    max_age_days: int = 45,
    refresh: bool = True,
) -> list[dict]:
    """Aggregate watchlist entries across all runs, newest-first, with status.

    Each entry carries the user overlay (``user_state`` = purchased/dismissed or
    None, plus ``user_state_at`` / ``user_note``) so the dashboard can file it.
    Entries older than max_age_days (by earnings date, fallback created), and
    ones the user dismissed, are classified without a price fetch to keep the
    scan cheap.
    """
    from tradingagents.reports_layout import iter_run_dirs

    root = Path(reports_root or "reports")
    states = load_states(root)
    today = date.today()

    # Collect first, fetch second: only rows that actually get a live refresh
    # are worth a network round-trip, and knowing the whole set up front lets
    # the fetches run concurrently instead of one-at-a-time inside the loop.
    rows: list[tuple[dict, bool, bool]] = []   # (entry, live, stale)
    for run_dir in iter_run_dirs(root):
        for entry in load_watchlist(run_dir):
            state = states.get(entry_key(run_dir.name, entry.get("ticker", ""))) or {}
            entry = {
                **entry,
                "run_id":        run_dir.name,
                "user_state":    state.get("state"),
                "user_state_at": state.get("at"),
                "user_note":     state.get("note"),
                "user_price":    state.get("price"),
            }
            ref = _to_date(entry.get("earnings_date")) or _to_date(entry.get("created"))
            stale = ref is not None and (today - ref).days > max_age_days
            live = refresh and not stale and entry["user_state"] != "dismissed"
            rows.append((entry, live, stale))

    # Warm both caches concurrently. Sequentially this was one round-trip per
    # ticker for the quote *and* one per entry for the daily bars (~150 ms each,
    # the dominant cost); refresh_entry below then hits the TTL caches.
    jobs: list = []
    for entry, live, _ in rows:
        if not live:
            continue
        ticker = str(entry.get("ticker", "")).strip().upper()
        if not ticker:
            continue
        jobs.append((fetch_live_price, (ticker,)))
        ed = _to_date(entry.get("earnings_date"))
        if ed is not None and today > ed:
            jobs.append((fetch_bars, (ticker, ed.isoformat())))
    if jobs:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(12, len(jobs))) as pool:
            list(pool.map(lambda job: job[0](*job[1]), jobs))

    out: list[dict] = []
    for entry, live, stale in rows:
        if not live:
            entry.update(entry_status(entry, today=today, bars=[] if stale else None))
            if stale and entry["status"] in ("ARMED", "PENDING"):
                entry["status"] = "EXPIRED"
            out.append(entry)
        else:
            out.append(refresh_entry(entry, today=today))
    return out


def _num(value):
    try:
        f = float(value)
        return f if f == f else None  # NaN guard
    except (TypeError, ValueError):
        return None
