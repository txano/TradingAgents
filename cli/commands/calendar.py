"""Earnings calendar command — fetch, filter, and hand off to the screener."""

from pathlib import Path
from typing import Optional

import questionary
import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

from cli.commands.common import _fetch_sector

console = Console()


# ── Market-cap helpers ────────────────────────────────────────────────────────

def _parse_cap_str(s: str) -> "int | None":
    """Parse market cap string from Nasdaq API (e.g. '$69,241,905,992') → int."""
    if not s or s in ("N/A", "--", ""):
        return None
    s = s.strip().replace("$", "").replace(",", "")
    multipliers = {"T": 1_000_000_000_000, "B": 1_000_000_000, "M": 1_000_000, "K": 1_000}
    for suffix, mult in multipliers.items():
        if s.upper().endswith(suffix):
            try:
                return int(float(s[:-1]) * mult)
            except ValueError:
                return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _parse_cap_threshold(s: str) -> int:
    """Parse user-supplied cap threshold like '2B', '500M', '1.5B' → int."""
    s = s.strip().upper()
    multipliers = {"T": 1_000_000_000_000, "B": 1_000_000_000, "M": 1_000_000, "K": 1_000}
    for suffix, mult in multipliers.items():
        if s.endswith(suffix):
            return int(float(s[:-1]) * mult)
    return int(float(s))


def _fmt_cap(n: "int | None") -> str:
    if n is None:
        return "[dim]N/A[/dim]"
    if n >= 1_000_000_000_000:
        return f"${n/1_000_000_000_000:.1f}T"
    if n >= 1_000_000_000:
        return f"${n/1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"${n/1_000_000:.0f}M"
    return f"${n:,}"


def _fmt_earnings_time(s: str) -> str:
    s = (s or "").lower()
    if "pre" in s or s == "bmo":
        return "Pre-mkt"
    if "after" in s or s in ("amc", "post"):
        return "After hrs"
    if "during" in s or s == "dmh":
        return "Intraday"
    return "?"


# ── Data fetchers ─────────────────────────────────────────────────────────────

def _fetch_earnings_nasdaq(date_str: str) -> list:
    """Fetch earnings calendar from Nasdaq public API. No API key required."""
    import requests
    url = f"https://api.nasdaq.com/api/calendar/earnings?date={date_str}"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Origin":  "https://www.nasdaq.com",
        "Referer": "https://www.nasdaq.com/",
    }
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    rows = (resp.json().get("data") or {}).get("rows") or []
    result = []
    for row in rows:
        sym = (row.get("symbol") or "").strip().upper()
        if not sym or sym == "N/A":
            continue
        result.append({
            "ticker":       sym,
            "company":      row.get("name", ""),
            "time":         _fmt_earnings_time(row.get("time", "")),
            "eps_estimate": row.get("epsForecast") or row.get("epsEstimate"),
            "market_cap":   _parse_cap_str(row.get("marketCap", "")),
        })
    return result


def _fetch_earnings_finnhub(date_str: str, api_key: str) -> list:
    """Fetch earnings calendar from Finnhub (API key required)."""
    import requests
    url = (
        f"https://finnhub.io/api/v1/calendar/earnings"
        f"?from={date_str}&to={date_str}&token={api_key}"
    )
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    cal = resp.json().get("earningsCalendar") or []
    result = []
    for item in cal:
        sym = (item.get("symbol") or "").strip().upper()
        if not sym:
            continue
        result.append({
            "ticker":       sym,
            "company":      "",
            "time":         _fmt_earnings_time(item.get("hour", "")),
            "eps_estimate": item.get("epsEstimate"),
            "market_cap":   None,
        })
    return result


def _enrich_market_caps(entries: list) -> None:
    """Fill market_cap for entries where it is None, using yfinance fast_info."""
    import yfinance as yf
    from concurrent.futures import ThreadPoolExecutor, as_completed

    missing = [e for e in entries if e["market_cap"] is None]
    if not missing:
        return

    def _fetch(entry):
        try:
            mc = yf.Ticker(entry["ticker"]).fast_info.market_cap
            return entry["ticker"], int(mc) if mc else None
        except Exception:
            return entry["ticker"], None

    lookup: dict = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = {ex.submit(_fetch, e): e["ticker"] for e in missing}
        for fut in as_completed(futures):
            ticker, mc = fut.result()
            lookup[ticker] = mc

    for e in entries:
        if e["ticker"] in lookup:
            e["market_cap"] = lookup[e["ticker"]]


# ── Command ───────────────────────────────────────────────────────────────────

def earnings_calendar(
    date:    Optional[str] = typer.Option(None,     "--date",    "-d", help="Earnings date YYYY-MM-DD (default: next weekday)"),
    source:  str           = typer.Option("nasdaq", "--source",  "-s", help="Data source: nasdaq | finnhub"),
    min_cap: Optional[str] = typer.Option(None,     "--min-cap", "-m", help="Minimum market cap e.g. 500M, 2B, 10B (prompted if omitted)"),
    all_caps: bool         = typer.Option(False,    "--all",     "-a", help="Show all tickers regardless of market cap"),
    budget:  int           = typer.Option(100_000,  "--budget",        help="Capital budget for screen run ($)"),
):
    """Fetch the earnings calendar for a date, filter by market cap, and optionally launch screening."""
    import os
    import datetime as dt

    console.print()
    console.print(Rule("[bold cyan]Earnings Calendar[/bold cyan]"))
    console.print()

    if date:
        try:
            dt.date.fromisoformat(date)
            date_str = date
        except ValueError:
            console.print(f"[red]Invalid date format '{date}'. Use YYYY-MM-DD.[/red]")
            raise typer.Exit(1)
    else:
        d = dt.date.today() + dt.timedelta(days=1)
        while d.weekday() >= 5:
            d += dt.timedelta(days=1)
        default_date = d.isoformat()
        date_str = typer.prompt("Date (YYYY-MM-DD)", default=default_date).strip()
        try:
            dt.date.fromisoformat(date_str)
        except ValueError:
            console.print(f"[red]Invalid date '{date_str}'.[/red]")
            raise typer.Exit(1)

    source_lower = source.lower()
    with console.status(f"[cyan]Fetching earnings calendar for {date_str} from {source_lower}…[/cyan]"):
        try:
            if source_lower == "finnhub":
                api_key = os.environ.get("FINNHUB_API_KEY", "").strip()
                if not api_key:
                    api_key = typer.prompt("Finnhub API key").strip()
                if not api_key:
                    console.print("[red]No Finnhub API key provided.[/red]")
                    raise typer.Exit(1)
                entries = _fetch_earnings_finnhub(date_str, api_key)
                console.print("[dim]Fetching market caps from yfinance (may take ~30s)…[/dim]")
                _enrich_market_caps(entries)
            else:
                entries = _fetch_earnings_nasdaq(date_str)
        except Exception as exc:
            console.print(f"[red]Failed to fetch calendar: {exc}[/red]")
            raise typer.Exit(1)

    if not entries:
        console.print(f"[yellow]No earnings found for {date_str}.[/yellow]")
        return

    if not all_caps and min_cap is None:
        _CAP_TIERS = [
            ("all",   "All caps",             0),
            ("200B",  "≥ $200B  Mega cap",    200_000_000_000),
            ("10B",   "≥ $10B   Large cap",   10_000_000_000),
            ("2B",    "≥ $2B    Mid + Large",  2_000_000_000),
            ("1B",    "≥ $1B    Mid cap+",     1_000_000_000),
            ("500M",  "≥ $500M  Small+",         500_000_000),
            ("custom","Custom…",              -1),
        ]
        cap_choices = []
        for key, label, threshold in _CAP_TIERS:
            if threshold == -1:
                cap_choices.append(questionary.Choice(f"  {label}", value=key))
            else:
                count = (
                    len(entries) if threshold == 0
                    else sum(1 for e in entries if (e["market_cap"] or 0) >= threshold)
                )
                cap_choices.append(
                    questionary.Choice(f"  {label:<26} ({count:>3} companies)", value=key)
                )

        cap_sel = questionary.select(
            "Minimum market cap:",
            choices=cap_choices,
            default=cap_choices[3],
            style=questionary.Style([("highlighted", "fg:cyan bold"), ("selected", "fg:cyan")]),
        ).ask()

        if cap_sel is None:
            return
        if cap_sel == "all":
            all_caps = True
            min_cap  = "0"
        elif cap_sel == "custom":
            min_cap = typer.prompt("Enter minimum market cap (e.g. 5B, 500M)").strip()
        else:
            min_cap = cap_sel

    try:
        cap_threshold = 0 if all_caps else _parse_cap_threshold(min_cap or "0")
    except ValueError:
        console.print(f"[red]Cannot parse --min-cap value '{min_cap}'. Use e.g. '1B', '500M'.[/red]")
        raise typer.Exit(1)

    filtered = sorted(
        [e for e in entries if all_caps or (e["market_cap"] is not None and e["market_cap"] >= cap_threshold)],
        key=lambda e: e["market_cap"] or 0,
        reverse=True,
    )

    cap_label = "all caps" if all_caps else f"market cap ≥ {min_cap}"
    console.print(Panel(
        f"[bold]Date:[/bold] {date_str}  |  [bold]Source:[/bold] {source_lower}  |  [bold]Filter:[/bold] {cap_label}\n"
        f"[bold]Total companies reporting:[/bold] {len(entries)}  |  [bold]Shown after filter:[/bold] {len(filtered)}",
        title="[bold cyan]Results[/bold cyan]",
        border_style="cyan",
        padding=(0, 2),
    ))
    console.print()

    if not filtered:
        console.print(
            f"[yellow]No companies with {cap_label} found for {date_str}.[/yellow]\n"
            "[dim]Try --min-cap 500M or --all to see everything.[/dim]"
        )
        return

    cal_tbl = Table(box=box.ROUNDED, show_lines=True)
    cal_tbl.add_column("#",          justify="right",   style="dim",       width=4)
    cal_tbl.add_column("Ticker",     justify="left",    style="cyan bold", width=8)
    cal_tbl.add_column("Company",    justify="left",                       width=32)
    cal_tbl.add_column("Time",       justify="center",                     width=11)
    cal_tbl.add_column("Market Cap", justify="right",                      width=12)
    cal_tbl.add_column("EPS Est.",   justify="right",                      width=10)

    for i, e in enumerate(filtered, 1):
        cap     = e["market_cap"]
        eps_str = str(e["eps_estimate"]) if e["eps_estimate"] not in (None, "N/A", "") else "[dim]N/A[/dim]"
        cap_color = (
            "bold green" if cap and cap >= 200_000_000_000 else
            "green"      if cap and cap >= 10_000_000_000  else
            "yellow"     if cap and cap >= 2_000_000_000   else
            "dim"
        )
        cal_tbl.add_row(
            str(i),
            e["ticker"],
            e["company"][:31] if e["company"] else "",
            e["time"] or "?",
            f"[{cap_color}]{_fmt_cap(cap)}[/{cap_color}]",
            eps_str,
        )

    console.print(cal_tbl)
    console.print()

    console.print(
        "[dim]Enter row numbers to screen (comma-separated), [bold]all[/bold] for all shown, "
        "or [bold]q[/bold] to quit:[/dim]"
    )
    raw = typer.prompt("").strip().lower()
    if not raw or raw == "q":
        return

    if raw == "all":
        selected = [e["ticker"] for e in filtered]
    else:
        selected = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                idx = int(part) - 1
                if 0 <= idx < len(filtered):
                    selected.append(filtered[idx]["ticker"])
                else:
                    console.print(f"[yellow]  Row {part} out of range, skipped.[/yellow]")
            except ValueError:
                console.print(f"[yellow]  '{part}' is not a valid row number, skipped.[/yellow]")
        seen: set = set()
        selected = [t for t in selected if not (t in seen or seen.add(t))]  # type: ignore[func-returns-value]

    if not selected:
        console.print("[yellow]No valid tickers selected.[/yellow]")
        return

    console.print(f"\n[bold]Selected ({len(selected)}):[/bold] [cyan]{', '.join(selected)}[/cyan]\n")
    go = typer.prompt("Launch screen with these tickers? [Y/n]", default="Y").strip().upper()
    if go not in ("Y", "YES", ""):
        console.print("[dim]Tickers not screened. Copy them manually if needed.[/dim]")
        return

    from cli.commands.screen import run_screening
    run_screening(budget=budget, tickers_prefill=selected)
