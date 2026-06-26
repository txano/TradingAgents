"""Peer earnings read-through (ROADMAP #9, playbook Block B).

When a ticker is about to report, its industry peers that *already reported this
season* are the single most predictive free signal the base system ignores. This
module pulls, for each peer that printed in the last ~month: EPS surprise and the
day-1 price reaction, and surfaces two patterns:

  * peers that **missed** on a shared driver → sector tailwind is weak (penalise)
  * peers that **beat and still fell** → the sector bar is elevated *regardless of
    the name* (the most dangerous pattern: good results, bad reaction → downgrade)

Peers come from a curated `PEER_MAP` (yfinance exposes no peer list), seeded with
the names we trade most in clustered industries (semis, solar, autos, restaurants,
LatAm fintech, miners, …). Unmapped tickers yield an empty read-through, stated
honestly rather than guessed. All network access reuses the guarded asymmetry
fetch, so failures degrade to "not available", never an exception.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

logger = logging.getLogger(__name__)

# Curated peer groups — clustered industries where read-through is well documented.
# Keys are uppercase tickers; values are peers reported within the lookback window.
PEER_MAP: dict[str, list[str]] = {
    # Semiconductors
    "AMD":  ["NVDA", "INTC", "AVGO", "QCOM", "MU"],
    "NVDA": ["AMD", "AVGO", "INTC", "MU", "TSM"],
    "MU":   ["WDC", "STX", "NVDA", "AMD"],
    "AVGO": ["NVDA", "AMD", "QCOM", "TXN"],
    "ARM":  ["NVDA", "AMD", "QCOM", "AVGO"],
    # Solar / clean energy
    "FSLR": ["ENPH", "SEDG", "RUN", "NXT"],
    "ENPH": ["FSLR", "SEDG", "RUN", "SHLS"],
    # Nuclear / SMR
    "SMR":  ["OKLO", "LEU", "CEG", "VST"],
    "OKLO": ["SMR", "LEU", "CEG"],
    # Hydrogen fuel cells
    "PLUG": ["BE", "BLDP", "FCEL"],
    # Energy E&P
    "OXY":  ["XOM", "CVX", "COP", "DVN"],
    # Restaurants
    "SBUX": ["MCD", "CMG", "YUM", "DRI"],
    "DRI":  ["SBUX", "CMG", "TXRH", "EAT"],
    "CMG":  ["SBUX", "MCD", "YUM", "DRI"],
    # Footwear / activewear
    "ONON": ["NKE", "DECK", "SKX", "CROX"],
    "DECK": ["NKE", "ONON", "SKX", "CROX"],
    # LatAm / digital banking + payments
    "NU":   ["MELI", "STNE", "PAGS", "SOFI"],
    "DLO":  ["STNE", "PAGS", "NU", "PYPL"],
    "MELI": ["AMZN", "SE", "NU", "STNE"],
    # Autos
    "HMC":  ["TM", "GM", "F", "STLA"],
    # Telecom
    "VZ":   ["T", "TMUS"],
    # Bitcoin miners
    "MARA": ["RIOT", "CLSK", "CIFR", "HUT"],
    # E-commerce enablement
    "GLBE": ["SHOP", "BIGC", "GLOB"],
    # Genomics / diagnostics
    "TEM":  ["GH", "EXAS", "NTRA"],
    # Steel
    "CMC":  ["NUE", "STLD", "X", "RS"],
    # Mega-cap tech / cloud
    "AMZN": ["GOOGL", "MSFT", "MELI", "SHOP"],
    "GOOGL": ["MSFT", "META", "AMZN"],
    # Packaged food
    "MKC":  ["GIS", "K", "CAG", "SJM"],
}


def get_peers(ticker: str) -> list[str]:
    """Return curated peers for a ticker (case-insensitive), or [] if unmapped."""
    return PEER_MAP.get((ticker or "").strip().upper(), [])


def beat_and_fell(rec: dict) -> bool:
    """True when a peer beat (or surprised positive) yet closed red on day 1.

    The elevated-bar signal: good results, bad reaction. Distinct from a miss.
    """
    move = rec.get("day1_move_pct")
    return bool(rec.get("beat")) and isinstance(move, (int, float)) and move < 0


# ---------------------------------------------------------------------------
# Pure aggregation (unit-tested directly, no network)
# ---------------------------------------------------------------------------

def assemble_readthrough(ticker: str, peer_records: list[dict], today: date,
                         lookback_days: int = 35) -> dict:
    """Aggregate per-peer prints into a read-through summary.

    `peer_records` is a list of {"peer", "date" (YYYY-MM-DD), "beat", "surprise_pct",
    "day1_move_pct"} — typically each peer's most recent past print. Keeps only
    those within `lookback_days` of `today`, sorts most-recent first, and computes:
      * sector_bar_elevated — any peer beat and still fell
      * any_miss            — any peer missed
    """
    recent: list[dict] = []
    for r in peer_records:
        d = r.get("date")
        if not d:
            continue
        try:
            pdate = datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        days_ago = (today - pdate).days
        if days_ago < 0 or days_ago > lookback_days:
            continue
        rec = dict(r)
        rec["days_ago"] = days_ago
        rec["beat_and_fell"] = beat_and_fell(rec)
        recent.append(rec)

    recent.sort(key=lambda r: r["days_ago"])
    return {
        "ticker": ticker,
        "lookback_days": lookback_days,
        "has_map": bool(get_peers(ticker)),
        "n_reported": len(recent),
        "peers": recent,
        "sector_bar_elevated": any(r["beat_and_fell"] for r in recent),
        "any_miss": any(r.get("beat") is False for r in recent),
    }


# ---------------------------------------------------------------------------
# Data fetch (reuses the guarded asymmetry fetch)
# ---------------------------------------------------------------------------

def _latest_past_print(peer: str) -> dict | None:
    """Most recent already-reported print for a peer, or None."""
    # Lazy import to avoid a circular import (allocation.__init__ → layer → peers).
    from tradingagents.allocation.asymmetry import fetch_earnings_reactions
    try:
        reactions = fetch_earnings_reactions(peer, n_prints=2)
    except Exception as exc:  # noqa: BLE001
        logger.debug("%s: peer fetch failed: %s", peer, exc)
        return None
    if not reactions:
        return None
    # fetch_earnings_reactions returns past prints, most recent first.
    top = reactions[0]
    return {
        "peer":          peer,
        "date":          top.get("date"),
        "beat":          top.get("beat"),
        "surprise_pct":  top.get("surprise_pct"),
        "day1_move_pct": top.get("day1_move_pct"),
    }


def build_peer_readthrough(ticker: str, lookback_days: int = 35,
                           today: date | None = None) -> dict:
    """Fetch peers' latest prints and assemble the read-through (guarded)."""
    today = today or date.today()
    peers = get_peers(ticker)
    records: list[dict] = []
    for peer in peers:
        rec = _latest_past_print(peer)
        if rec is not None:
            records.append(rec)
    return assemble_readthrough(ticker, records, today, lookback_days=lookback_days)


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def _peer_line(r: dict) -> str:
    surp = r.get("surprise_pct")
    move = r.get("day1_move_pct")
    surp_s = f"{surp:+.1f}%" if isinstance(surp, (int, float)) else "n/a"
    move_s = f"{move:+.1f}%" if isinstance(move, (int, float)) else "n/a"
    verdict = "Beat" if r.get("beat") else ("Miss" if r.get("beat") is False else "?")
    note = ""
    if r.get("beat_and_fell"):
        note = " — BEAT-AND-FELL (elevated bar)"
    elif r.get("beat") is False:
        note = " — miss"
    return (f"- {r['peer']} ({r['days_ago']}d ago): {verdict}, surprise {surp_s}, "
            f"day-1 {move_s}{note}")


def format_peer_readthrough(data: dict | None) -> str:
    """Markdown PEER READ-THROUGH block for the earnings-brief prompt."""
    if not data or not data.get("has_map"):
        return "No curated peer map for this ticker."
    if not data.get("n_reported"):
        return (f"No peers reported in the last {data.get('lookback_days', 35)} days "
                f"(checked: {', '.join(get_peers(data['ticker'])) or 'none'}).")

    lines = [_peer_line(r) for r in data["peers"]]
    if data.get("sector_bar_elevated"):
        lines.append(
            "- ⚠ At least one peer BEAT AND STILL FELL — the sector bar is elevated; "
            "downgrade conviction one notch even on a likely beat."
        )
    elif data.get("any_miss"):
        lines.append("- ⚠ A peer missed on a shared driver — treat the sector tailwind as weak.")
    return "\n".join(lines)


def format_peer_oneliner(data: dict | None) -> str:
    """Compact one-liner for the council ticker section."""
    if not data or not data.get("has_map"):
        return "No peer map"
    if not data.get("n_reported"):
        return "No peers reported recently"
    parts = []
    for r in data["peers"][:4]:
        move = r.get("day1_move_pct")
        move_s = f"{move:+.0f}%" if isinstance(move, (int, float)) else "n/a"
        tag = "B" if r.get("beat") else ("M" if r.get("beat") is False else "?")
        flag = "!" if r.get("beat_and_fell") else ""
        parts.append(f"{r['peer']}{tag}{move_s}{flag}")
    summary = " · ".join(parts)
    if data.get("sector_bar_elevated"):
        summary += " | ⚠ elevated bar (peer beat-and-fell)"
    return summary
