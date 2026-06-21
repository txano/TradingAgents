#!/usr/bin/env python3
"""
earnings_toolkit.py — Pre-earnings options snapshot via Interactive Brokers.

Pulls, for a given ticker and earnings date:
  * implied move (ATM straddle / spot) for the first expiry covering the print
  * ATM implied volatility (event expiry and next monthly) -> term-structure ratio
  * ~25-delta put/call IV skew on the event expiry
  * realized-vol percentile of the underlying (interim proxy until your own
    IV history accumulates in the log)
and appends a row to earnings_log.csv so coverage ratios and per-ticker
reaction stats accumulate automatically.

Requirements:
  pip install ib_async pandas numpy
  A running TWS or IB Gateway with API enabled.
    TWS live: 7496 | TWS paper: 7497 | Gateway live: 4001 | Gateway paper: 4002

Usage:
  python earnings_toolkit.py NVDA 2026-08-26
  python earnings_toolkit.py NVDA 2026-08-26 --host 127.0.0.1 --port 7497 --log my_log.csv
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from datetime import datetime, date
from pathlib import Path

import numpy as np

try:
    from ib_async import IB, Stock, Option, util
except ImportError:  # graceful message instead of a stack trace
    sys.exit("ib_async not installed. Run: pip install ib_async")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def mid(ticker) -> float | None:
    """Best-effort mid price from a ticker snapshot."""
    bid = ticker.bid if ticker.bid and ticker.bid > 0 else None
    ask = ticker.ask if ticker.ask and ticker.ask > 0 else None
    if bid and ask:
        return (bid + ask) / 2
    if ticker.last and ticker.last > 0:
        return ticker.last
    if ticker.close and ticker.close > 0:
        return ticker.close
    return None


def iv_of(ticker) -> float | None:
    """Implied vol from modelGreeks (populated by generic tick 106)."""
    g = ticker.modelGreeks
    if g and g.impliedVol and not math.isnan(g.impliedVol):
        return g.impliedVol
    return None


def delta_of(ticker) -> float | None:
    g = ticker.modelGreeks
    if g and g.delta is not None and not math.isnan(g.delta):
        return g.delta
    return None


def first_expiry_on_or_after(expirations: list[str], when: date) -> str | None:
    """Expirations come as YYYYMMDD strings."""
    for exp in sorted(expirations):
        if datetime.strptime(exp, "%Y%m%d").date() >= when:
            return exp
    return None


def next_monthly_after(expirations: list[str], event_expiry: str) -> str | None:
    """First standard monthly (3rd Friday) expiry strictly after the event expiry."""
    event_dt = datetime.strptime(event_expiry, "%Y%m%d").date()
    for exp in sorted(expirations):
        d = datetime.strptime(exp, "%Y%m%d").date()
        if d > event_dt and d.weekday() == 4 and 15 <= d.day <= 21:
            return exp
    # fallback: anything at least 20 calendar days out
    for exp in sorted(expirations):
        d = datetime.strptime(exp, "%Y%m%d").date()
        if (d - event_dt).days >= 20:
            return exp
    return None


def realized_vol_percentile(ib: IB, stock: Stock, window: int = 30) -> float | None:
    """
    Percentile of current 30d realized vol within its 1y range.
    Interim stand-in for IV rank until logged IV history accumulates.
    """
    bars = ib.reqHistoricalData(
        stock, endDateTime="", durationStr="1 Y", barSizeSetting="1 day",
        whatToShow="TRADES", useRTH=True, formatDate=1,
    )
    if not bars or len(bars) < window + 30:
        return None
    closes = np.array([b.close for b in bars], dtype=float)
    rets = np.diff(np.log(closes))
    ann = math.sqrt(252)
    rv = np.array([rets[i - window:i].std() * ann for i in range(window, len(rets) + 1)])
    if rv.size < 2 or rv.max() == rv.min():
        return None
    return float((rv[-1] - rv.min()) / (rv.max() - rv.min()))


def snapshot_options(ib: IB, contracts: list[Option], wait_s: float = 8.0):
    """Request snapshots (incl. generic tick 106 for IV) and wait for greeks."""
    tickers = [ib.reqMktData(c, genericTickList="106", snapshot=False) for c in contracts]
    ib.sleep(wait_s)
    return tickers


# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------

def analyze(symbol: str, earnings_date: date, host: str, port: int,
            client_id: int, log_path: Path) -> dict:
    ib = IB()
    ib.connect(host, port, clientId=client_id, timeout=15)
    try:
        stock = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(stock)

        # ---- spot -------------------------------------------------------
        st = ib.reqMktData(stock, snapshot=False)
        ib.sleep(3)
        spot = mid(st)
        if not spot:
            raise RuntimeError(f"No spot price for {symbol} (market data subscription?)")

        # ---- chain ------------------------------------------------------
        chains = ib.reqSecDefOptParams(stock.symbol, "", stock.secType, stock.conId)
        chain = next((c for c in chains if c.exchange == "SMART"), chains[0] if chains else None)
        if chain is None:
            raise RuntimeError(f"No option chain for {symbol}")

        event_exp = first_expiry_on_or_after(list(chain.expirations), earnings_date)
        if event_exp is None:
            raise RuntimeError("No expiration on/after the earnings date")
        monthly_exp = next_monthly_after(list(chain.expirations), event_exp)

        strikes = sorted(s for s in chain.strikes if 0.6 * spot <= s <= 1.4 * spot)
        atm = min(strikes, key=lambda s: abs(s - spot))

        # ---- ATM straddle on event expiry --------------------------------
        atm_call = Option(symbol, event_exp, atm, "C", "SMART", currency="USD")
        atm_put = Option(symbol, event_exp, atm, "P", "SMART", currency="USD")
        legs = [atm_call, atm_put]

        # candidates for ~25-delta skew: OTM strikes around ±10% of spot
        otm_call_strikes = [s for s in strikes if s > spot][:8]
        otm_put_strikes = [s for s in strikes if s < spot][-8:]
        skew_calls = [Option(symbol, event_exp, s, "C", "SMART", currency="USD") for s in otm_call_strikes]
        skew_puts = [Option(symbol, event_exp, s, "P", "SMART", currency="USD") for s in otm_put_strikes]

        monthly_legs = []
        if monthly_exp:
            m_atm = min(strikes, key=lambda s: abs(s - spot))
            monthly_legs = [Option(symbol, monthly_exp, m_atm, "C", "SMART", currency="USD")]

        all_opts = legs + skew_calls + skew_puts + monthly_legs
        all_opts = [c for c in ib.qualifyContracts(*all_opts) if c.conId]
        tickers = snapshot_options(ib, all_opts)
        by_key = {(t.contract.lastTradeDateOrContractMonth, t.contract.strike, t.contract.right): t
                  for t in tickers}

        tc = by_key.get((event_exp, atm, "C"))
        tp = by_key.get((event_exp, atm, "P"))
        call_mid, put_mid = (mid(tc) if tc else None), (mid(tp) if tp else None)
        implied_move = ((call_mid + put_mid) / spot) if (call_mid and put_mid) else None
        atm_iv_event = next((v for v in (iv_of(tc) if tc else None, iv_of(tp) if tp else None) if v), None)

        # ---- ~25-delta skew ----------------------------------------------
        def closest_to_delta(cands, target):
            best, best_err = None, 1.0
            for t in cands:
                d = delta_of(t)
                if d is None:
                    continue
                err = abs(abs(d) - target)
                if err < best_err:
                    best, best_err = t, err
            return best

        c25 = closest_to_delta([by_key.get((event_exp, s, "C")) for s in otm_call_strikes
                                if by_key.get((event_exp, s, "C"))], 0.25)
        p25 = closest_to_delta([by_key.get((event_exp, s, "P")) for s in otm_put_strikes
                                if by_key.get((event_exp, s, "P"))], 0.25)
        skew = None
        if c25 and p25 and iv_of(p25) and iv_of(c25):
            skew = iv_of(p25) - iv_of(c25)

        # ---- term structure ----------------------------------------------
        term_ratio = None
        if monthly_legs:
            tm = by_key.get((monthly_exp, monthly_legs[0].strike, "C"))
            iv_m = iv_of(tm) if tm else None
            if atm_iv_event and iv_m:
                term_ratio = atm_iv_event / iv_m

        rv_pct = realized_vol_percentile(ib, stock)

        row = {
            "snapshot_ts": datetime.now().isoformat(timespec="seconds"),
            "symbol": symbol,
            "earnings_date": earnings_date.isoformat(),
            "event_expiry": event_exp,
            "spot": round(spot, 2),
            "atm_strike": atm,
            "implied_move_pct": round(implied_move * 100, 2) if implied_move else None,
            "atm_iv_event": round(atm_iv_event * 100, 1) if atm_iv_event else None,
            "term_ratio": round(term_ratio, 2) if term_ratio else None,
            "skew_25d_pts": round(skew * 100, 1) if skew is not None else None,
            "realized_vol_pctile": round(rv_pct, 2) if rv_pct is not None else None,
            # post-print columns, filled later by you / a follow-up script:
            "move_d1": "", "move_d5": "", "move_d20": "", "coverage_ratio": "",
            "beat_eps": "", "beat_rev": "", "guide": "", "gate_path": "", "action": "", "pnl_final": "",
        }
        append_log(log_path, row)
        return row
    finally:
        ib.disconnect()


def append_log(path: Path, row: dict) -> None:
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new:
            w.writeheader()
        w.writerow(row)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Pre-earnings options snapshot via IBKR")
    p.add_argument("symbol", help="Ticker, e.g. NVDA")
    p.add_argument("earnings_date", help="YYYY-MM-DD")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7497, help="7496/7497 TWS, 4001/4002 Gateway")
    p.add_argument("--client-id", type=int, default=42)
    p.add_argument("--log", default="earnings_log.csv")
    a = p.parse_args()

    ed = datetime.strptime(a.earnings_date, "%Y-%m-%d").date()
    row = analyze(a.symbol.upper(), ed, a.host, a.port, a.client_id, Path(a.log))

    print(f"\n{a.symbol.upper()} — earnings {ed}  (event expiry {row['event_expiry']})")
    print(f"  Spot:               {row['spot']}")
    print(f"  Implied move:       ±{row['implied_move_pct']}%")
    print(f"  ATM IV (event):     {row['atm_iv_event']}%")
    print(f"  Term ratio (evt/m): {row['term_ratio']}   (>1.4 = heavy event premium)")
    print(f"  25Δ skew (P−C):     {row['skew_25d_pts']} vol pts")
    print(f"  RVol percentile:    {row['realized_vol_pctile']}  (IV-rank proxy until log builds)")
    print(f"\nLogged to {a.log}. Fill move_d1/d5/d20 after the print to build coverage stats.")


if __name__ == "__main__":
    main()
