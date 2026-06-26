"""IBKR trade import command."""

import datetime
import json as _json
import os
import zipfile
from pathlib import Path
from typing import Optional

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

from cli.commands.common import _fetch_sector, _trades_path
from tradingagents.trade_log import backfill as _backfill_trades, ensure_v2

console = Console()

# When a trade's entry (purchase) date is unknown, the screened-within gate falls
# back to this wider window measured from the exit date.
_EXIT_FALLBACK_DAYS = 30


def _screened_within(trade: dict, days: int, reports_dir: Path) -> bool:
    """True if the ticker was screened within `days` before it was purchased.

    Uses the entry (open) date when available and requires a screening run dated
    within `days` before it. When the entry date is unknown (not in the Flex
    record), falls back to requiring a screen within max(days, 30) before the
    exit date. Either way the screen must *predate* the trade.
    """
    import datetime as _dt
    from tradingagents.trade_log import _run_date, find_screening_run

    ticker = trade.get("ticker", "")
    entry = trade.get("entry_date") or ""
    exit_ = trade.get("exit_date") or ""
    if entry:
        ref, window = entry, days
    elif exit_:
        ref, window = exit_, max(days, _EXIT_FALLBACK_DAYS)
    else:
        return False

    ticker_dir = find_screening_run(ticker, ref, reports_dir)
    if ticker_dir is None:
        return False
    run_date = _run_date(ticker_dir.parent.name)
    if not run_date:
        return False
    try:
        delta = (_dt.date.fromisoformat(ref) - _dt.date.fromisoformat(run_date)).days
    except ValueError:
        return False
    return 0 <= delta <= window


def _get_analyzed_tickers(reports_dir: Path) -> "dict[str, str]":
    """Return mapping of ticker → earliest analysis date (YYYY-MM-DD string).

    Scans the current layout (reports/earnings/{screening,earnings}_*/TICKER/
    and reports/analysis/TICKER_YYYYMMDD_HHMMSS/), plus the legacy root-level
    layout (reports/screening_*/, reports/screening_*.zip, and
    reports/TICKER_YYYYMMDD_HHMMSS/).
    """
    ticker_dates: dict = {}
    if not reports_dir.exists():
        return ticker_dates

    def _keep_earliest(ticker: str, date_str: str) -> None:
        existing = ticker_dates.get(ticker)
        if existing is None or date_str < existing:
            ticker_dates[ticker] = date_str

    from datetime import datetime as _dt

    def _run_date(d: Path) -> "str | None":
        """Analysis date for a run dir: metadata.json's run_at, else the
        trailing YYYYMMDD_HHMMSS timestamp in the dir name."""
        meta_path = d / "metadata.json"
        if meta_path.exists():
            try:
                run_at = _json.loads(meta_path.read_text(encoding="utf-8")).get("run_at", "")
                if run_at:
                    return run_at[:10]
            except Exception:
                pass
        parts = d.name.split("_")
        if len(parts) < 2:
            return None
        try:
            return _dt.strptime("_".join(parts[-2:]), "%Y%m%d_%H%M%S").strftime("%Y-%m-%d")
        except ValueError:
            return None

    # Current layout: reports/earnings/{screening,earnings}_*/TICKER/
    earnings_base = reports_dir / "earnings"
    if earnings_base.is_dir():
        for d in earnings_base.iterdir():
            if not d.is_dir() or not (d.name.startswith("screening_") or d.name.startswith("earnings_")):
                continue
            date_str = _run_date(d)
            if date_str is None:
                continue
            for t in d.iterdir():
                if t.is_dir() and not t.suffix:
                    _keep_earliest(t.name, date_str)

    # Current layout: reports/analysis/TICKER_YYYYMMDD_HHMMSS/
    analysis_base = reports_dir / "analysis"
    if analysis_base.is_dir():
        for d in analysis_base.iterdir():
            if not d.is_dir():
                continue
            parts = d.name.split("_")
            if len(parts) < 3:
                continue
            try:
                date_str = _dt.strptime("_".join(parts[-2:]), "%Y%m%d_%H%M%S").strftime("%Y-%m-%d")
            except ValueError:
                continue
            _keep_earliest(parts[0], date_str)

    # Legacy: unzipped screening directories: reports/screening_YYYY-MM-DD_*/
    for d in reports_dir.glob("screening_*/"):
        if not d.is_dir():
            continue
        parts = d.name.split("_")
        try:
            date_str = parts[1]
            _dt.strptime(date_str, "%Y-%m-%d")
        except (IndexError, ValueError):
            continue
        for t in d.iterdir():
            if t.is_dir() and not t.suffix:
                _keep_earliest(t.name, date_str)

    # Legacy: zipped screening archives: reports/screening_YYYY-MM-DD_*.zip
    for zp in reports_dir.glob("screening_*.zip"):
        parts = zp.stem.split("_")
        try:
            date_str = parts[1]
            _dt.strptime(date_str, "%Y-%m-%d")
        except (IndexError, ValueError):
            continue
        try:
            with zipfile.ZipFile(zp, "r") as zf:
                for name in zf.namelist():
                    segments = name.strip("/").split("/")
                    # Need at least: root_dir/TICKER
                    if len(segments) < 2 or not segments[1]:
                        continue
                    # Skip macOS metadata dirs (__MACOSX) and dot-file roots
                    if segments[0] == "__MACOSX" or segments[0].startswith("._"):
                        continue
                    ticker = segments[1]
                    # Skip macOS dot files and any file entries (have an extension)
                    if ticker.startswith("._") or "." in ticker:
                        continue
                    _keep_earliest(ticker, date_str)
        except zipfile.BadZipFile:
            continue

    # Legacy: individual analysis dirs: reports/TICKER_YYYYMMDD_HHMMSS/
    for d in reports_dir.iterdir():
        if (d.is_dir() and "_" in d.name
                and not d.name.startswith("screening_")
                and not d.name.startswith("reflections")):
            parts = d.name.split("_")
            try:
                date_str = _dt.strptime(parts[1], "%Y%m%d").strftime("%Y-%m-%d")
            except (IndexError, ValueError):
                continue
            _keep_earliest(parts[0], date_str)

    return ticker_dates


def import_ibkr(
    file: Optional[str] = typer.Option(None, "--file", "-f", help="Path to a downloaded Flex XML report (skips API call)"),
    all_trades: bool = typer.Option(False, "--all", help="Import all trades, not just TradingAgents-analyzed tickers"),
    query_id: Optional[str] = typer.Option(None, "--query-id", help="Override IBKR_FLEX_QUERY_ID (e.g. a lighter, shorter-range query)"),
    debug: bool = typer.Option(False, "--debug", help="Print the raw IBKR Flex responses for troubleshooting"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt — auto-import (for cron/automation)"),
    screened_within: Optional[int] = typer.Option(
        None, "--screened-within",
        help="Only import trades whose ticker was screened within N days before purchase "
             "(entry date; falls back to within ~30 days before exit when entry is unknown)",
    ),
):
    """Import closed trades from IBKR.

    By default only imports tickers that have been screened or analyzed by
    TradingAgents. Use --all to import everything.

    Two modes:
      --file path/to/report.xml   Parse a manually downloaded Flex XML file.
      (no flag)                   Download automatically via the Flex API
                                  (requires IBKR_FLEX_TOKEN + IBKR_FLEX_QUERY_ID in .env).
    """
    from tradingagents.ibkr import download_flex_xml, parse_closing_trades

    console.print()
    console.print(Rule("[bold cyan]IBKR Trade Import[/bold cyan]"))
    console.print()

    xml_str = None

    if file:
        xml_path = Path(file)
        if not xml_path.exists():
            console.print(f"[red]File not found: {xml_path}[/red]")
            return
        xml_str = xml_path.read_text(encoding="utf-8")
        console.print(f"[dim]Reading: {xml_path.resolve()}[/dim]\n")
    else:
        token    = os.environ.get("IBKR_FLEX_TOKEN", "").strip()
        query_id = (query_id or os.environ.get("IBKR_FLEX_QUERY_ID", "")).strip()

        if not token or not query_id:
            console.print(Panel(
                "[yellow]No --file provided and no API credentials found.[/yellow]\n\n"
                "[bold]Option A — manual file (recommended):[/bold]\n"
                "  1. Go to IBKR portal → Performance & Reports → Flex Queries\n"
                "  2. Click the [bold]→[/bold] (run) button next to your TradingAgents query\n"
                "  3. Download the XML report\n"
                "  4. Run: [bold]uv run tradingagents import-ibkr --file path/to/report.xml[/bold]\n\n"
                "[bold]Option B — automatic API:[/bold]\n"
                "  Add to [bold].env[/bold]:\n"
                "    IBKR_FLEX_TOKEN=your_token\n"
                "    IBKR_FLEX_QUERY_ID=1495116",
                border_style="yellow",
                padding=(1, 2),
            ))
            return

        console.print(f"[dim]Connecting to IBKR Flex Web Service (query {query_id})...[/dim]")
        dbg = (lambda m: console.print(f"[dim cyan]IBKR ▸ {m}[/dim cyan]")) if debug else None

        def _download():
            # In debug mode print plainly (no spinner) so the raw responses are readable.
            if debug:
                return download_flex_xml(
                    token, query_id,
                    progress=lambda m: console.print(f"[dim]{m}[/dim]"), debug=dbg,
                )
            with console.status("[bold yellow]Requesting report from IBKR…[/bold yellow]") as status:
                return download_flex_xml(
                    token, query_id,
                    progress=lambda m: status.update(f"[bold yellow]{m}[/bold yellow]"),
                )

        try:
            xml_str = _download()
        except Exception as exc:
            console.print(f"[red]Download failed: {exc}[/red]")
            console.print(
                "[dim]Tips:[/dim]\n"
                "  [dim]• Re-run with[/dim] [bold]--debug[/bold] [dim]to see exactly what IBKR returned.[/dim]\n"
                "  [dim]• If the query is large, set a shorter date range (e.g. Last 30 Days) in the IBKR "
                "portal, or point at a lighter query with[/dim] [bold]--query-id <id>[/bold][dim]. "
                "Duplicate trades are skipped on import, so a short rolling window is enough.[/dim]\n"
                "  [dim]• Or download the XML manually and run:[/dim] "
                "[bold]uv run tradingagents import-ibkr --file path/to/report.xml[/bold]"
            )
            # Non-zero exit so a scheduled (systemd/cron) run is flagged as failed.
            # Note: with a 30-day query and a 15-day cadence the next run still
            # covers this window (dedup prevents duplicates), so a single failure
            # is self-healing.
            raise typer.Exit(code=1)

    try:
        ibkr_trades = parse_closing_trades(xml_str)
    except Exception as exc:
        console.print(f"[red]Parse error: {exc}[/red]")
        raise typer.Exit(code=1)

    if not ibkr_trades:
        console.print("[yellow]No closing stock trades found in the report.[/yellow]")
        return

    if not all_trades:
        before = len(ibkr_trades)

        if screened_within is not None:
            # Strict gate: ticker must have been screened within N days before purchase.
            console.print(
                f"[dim]Gating to trades screened within {screened_within} day(s) before purchase "
                f"(entry date; ≤{max(screened_within, _EXIT_FALLBACK_DAYS)}d before exit when entry is unknown).[/dim]"
            )
            _reports = Path("reports")
            ibkr_trades = [t for t in ibkr_trades if _screened_within(t, screened_within, _reports)]
            reason = "no qualifying screen within the window before purchase"
        else:
            analyzed = _get_analyzed_tickers(Path("reports"))
            if analyzed:
                console.print(
                    f"[dim]Filtering to {len(analyzed)} TradingAgents-analyzed ticker(s): "
                    f"{', '.join(sorted(analyzed)[:10])}"
                    f"{'…' if len(analyzed) > 10 else ''}[/dim]"
                )
            else:
                console.print(
                    "[yellow]No TradingAgents analysis reports found in reports/ "
                    "(screening dirs may be zipped or missing). "
                    "Importing all trades — use --all to suppress this notice.[/yellow]\n"
                )

            def _should_import(t: dict) -> bool:
                if not analyzed:
                    return True  # nothing to filter against — import all
                ticker = t.get("ticker", "")
                analysis_date = analyzed.get(ticker)
                if analysis_date is None:
                    return False
                exit_date = t.get("exit_date", "")
                return bool(exit_date) and exit_date >= analysis_date

            ibkr_trades = [t for t in ibkr_trades if _should_import(t)]
            reason = "ticker not analyzed by TradingAgents, or traded before analysis date"

        filtered_out = before - len(ibkr_trades)
        if filtered_out:
            console.print(
                f"[dim]Filtered out {filtered_out} trade(s) ({reason}). "
                f"Use --all to import everything.[/dim]\n"
            )
        if not ibkr_trades:
            console.print("[yellow]No trades remain after filtering. Use --all to import everything.[/yellow]")
            return

    console.print(f"[green]Found {len(ibkr_trades)} closing trade(s) in the report.[/green]\n")

    trade_log_path = _trades_path()
    trade_log_path.parent.mkdir(parents=True, exist_ok=True)
    existing_trades = []
    if trade_log_path.exists():
        try:
            existing_trades = _json.loads(trade_log_path.read_text(encoding="utf-8"))
        except Exception:
            existing_trades = []

    existing_ids = {
        t.get("ibkr_trade_id") for t in existing_trades if t.get("ibkr_trade_id")
    }

    new_trades = [t for t in ibkr_trades if t.get("ibkr_trade_id") not in existing_ids]
    skipped = len(ibkr_trades) - len(new_trades)

    if skipped:
        console.print(f"[dim]Skipping {skipped} already-imported trade(s).[/dim]\n")

    if not new_trades:
        console.print("[green]All trades already imported. Nothing new to add.[/green]")
        return

    prev = Table(box=box.ROUNDED, title=f"[bold]New Trades to Import ({len(new_trades)})[/bold]", show_lines=True)
    prev.add_column("Ticker",    style="cyan bold", width=8)
    prev.add_column("Direction", justify="center",  width=10)
    prev.add_column("Shares",    justify="right",   width=8)
    prev.add_column("Entry",     justify="right",   width=9)
    prev.add_column("Exit",      justify="right",   width=9)
    prev.add_column("P&L",       justify="right",   width=11)
    prev.add_column("P&L %",     justify="right",   width=8)
    prev.add_column("Outcome",   justify="center",  width=10)
    prev.add_column("Exit Date", width=12)
    prev.add_column("CCY",       width=5)

    for t in new_trades:
        pnl       = t.get("pnl", 0)
        pnl_pct   = t.get("pnl_pct", 0)
        direction = t.get("direction", "?")
        outcome   = t.get("outcome", "?")
        pnl_color = "green" if pnl >= 0 else "red"
        dir_color = {"BUY": "green", "SHORT": "red"}.get(direction, "white")
        out_color = {"WIN": "green", "LOSS": "red", "BREAK_EVEN": "yellow"}.get(outcome, "white")
        prev.add_row(
            t.get("ticker", "?"),
            f"[{dir_color}]{direction}[/{dir_color}]",
            f"{t.get('shares', 0):.0f}",
            f"${t.get('entry_price', 0):.2f}",
            f"${t.get('exit_price', 0):.2f}",
            f"[{pnl_color}]${pnl:+.2f}[/{pnl_color}]",
            f"[{pnl_color}]{pnl_pct:+.1f}%[/{pnl_color}]",
            f"[{out_color}]{outcome}[/{out_color}]",
            t.get("exit_date", "?"),
            t.get("currency", "USD"),
        )
    console.print(prev)
    console.print()

    if yes:
        console.print("[dim]--yes given — importing without confirmation.[/dim]")
    else:
        confirm = typer.prompt("Import these trades into trades.json?", default="Y").strip().upper()
        if confirm not in ("Y", "YES", ""):
            console.print("[yellow]Import cancelled.[/yellow]")
            return

    now = datetime.datetime.now().isoformat()
    added = 0
    for t in new_trades:
        ticker = t["ticker"]
        sector = _fetch_sector(ticker)
        entry = {
            "ticker":                  ticker,
            "sector":                  sector,
            "direction":               t["direction"],
            "shares":                  t["shares"],
            "entry_price":             t["entry_price"],
            "exit_price":              t["exit_price"],
            "pnl":                     t["pnl"],
            "pnl_pct":                 t["pnl_pct"],
            "outcome":                 t["outcome"],
            "prediction_accuracy":     None,
            "beat_prediction_correct": None,
            "guidance_prediction_correct": None,
            "key_lesson":              "",
            "trade_date":              t.get("entry_date") or None,
            "exit_date":               t["exit_date"],
            "screening_run":           None,
            "analysis_path":           None,
            "reflection_path":         None,
            "source":                  "ibkr",
            "currency":                t.get("currency", "USD"),
            "ibkr_trade_id":           t.get("ibkr_trade_id"),
            "ibkr_exec_id":            t.get("ibkr_exec_id"),
            "logged_at":               now,
        }
        ensure_v2(entry)  # schema-v2 fields (null); 'backfill-trades' fills them
        existing_trades.append(entry)
        added += 1

    trade_log_path.write_text(_json.dumps(existing_trades, indent=2), encoding="utf-8")
    console.print(f"\n[green]✓ Imported {added} trade(s) → {trade_log_path}[/green]")
    console.print(
        "[dim]Note: trade_date (entry date) is not available from the Flex closing record. "
        "Run 'reflect' on any trade to add full analysis context.[/dim]\n"
    )


def backfill_trades(
    no_network: bool = typer.Option(
        False, "--no-network", help="Artifact-only pass (skip the yfinance outcome/reaction fetch)"
    ),
    ticker: Optional[str] = typer.Option(
        None, "--ticker", "-t", help="Only enrich this ticker"
    ),
):
    """Backfill the schema-v2 context/outcome/reaction fields on trades.json (#17).

    Links each trade to the screening run it came from and fills T-1 context
    (implied move, run-up, 52w distance, revision direction, coverage) from the
    saved pricing/crowding/asymmetry artifacts, plus the realised outcome and
    day-1/5/20 reaction from yfinance. Idempotent — safe to re-run any time.
    """
    console.print()
    console.print(Rule("[bold cyan]Trade-log Backfill (schema v2)[/bold cyan]"))
    console.print()

    trade_log_path = _trades_path()
    if not trade_log_path.exists():
        console.print("[yellow]No trades.json found — nothing to backfill.[/yellow]")
        return
    try:
        trades = _json.loads(trade_log_path.read_text(encoding="utf-8"))
    except Exception as exc:
        console.print(f"[red]Could not read {trade_log_path}: {exc}[/red]")
        return

    tickers = {ticker.upper()} if ticker else None
    with console.status("[bold yellow]Enriching trades…[/bold yellow]"):
        summary = _backfill_trades(
            trades, reports_dir=Path("reports"),
            with_network=not no_network, tickers=tickers,
        )

    trade_log_path.write_text(_json.dumps(trades, indent=2), encoding="utf-8")
    console.print(
        f"[green]✓ Backfilled {summary['total']} trade(s)[/green]  "
        f"[dim](newly linked to a screening run: {summary['newly_linked']}; "
        f"newly filled outcome/reaction: {summary['newly_filled_outcome']})[/dim]"
    )
    console.print(f"[dim]→ {trade_log_path}[/dim]\n")
