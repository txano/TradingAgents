"""Allocate and allocation-weights commands."""

import json as _json
import re as _re
from pathlib import Path
from typing import Optional

import questionary
import typer
from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.allocation import AllocationLayer
from tradingagents.allocation.layer import build_advisor_llms, parse_allocation
from cli.utils import (
    select_llm_provider, select_deep_thinking_agent,
    ask_gemini_thinking_config, ask_openai_reasoning_effort, ask_anthropic_effort,
)
from cli.commands.common import _fetch_sector

console = Console()


def allocate(
    budget: int = typer.Option(100_000, "--budget", help="Capital budget for allocation ($)"),
    dir: Optional[str] = typer.Option(None, "--dir", "-d", help="Path to screening directory (skips interactive picker)"),
):
    """Rebuild the screening table and re-run the Allocation Manager.

    Reads scores directly from each ticker's earnings_brief.md, regenerates
    screening_table.md, then runs allocation. Useful when combining tickers
    from multiple sessions or correcting a bad table.
    """
    reports_dir = Path("reports")

    if dir:
        screening_dir = Path(dir)
    else:
        candidates = sorted(
            [d for d in reports_dir.glob("screening_*/") if d.is_dir()],
            reverse=True,
        )
        if not candidates:
            console.print("[red]No screening_* directories found in reports/.[/red]")
            raise typer.Exit(1)

        console.print("\n[bold]Select a screening directory:[/bold]\n")
        for i, d in enumerate(candidates, 1):
            ticker_dirs = [t for t in d.iterdir() if t.is_dir() and (t / "earnings_brief.md").exists()]
            alloc_tag = "[dim](allocation exists)[/dim]" if (d / "allocation.md").exists() else ""
            console.print(f"  [cyan]{i}.[/cyan] {d.name}  [dim]{len(ticker_dirs)} tickers[/dim] {alloc_tag}")

        console.print()
        choice = questionary.text("Enter number:").ask()
        if not choice:
            raise typer.Exit(0)
        try:
            n = int(choice.strip())
            screening_dir = candidates[n - 1]
        except (ValueError, IndexError):
            console.print("[red]Invalid selection.[/red]")
            raise typer.Exit(1)

    if not screening_dir.exists():
        console.print(f"[red]Directory not found: {screening_dir}[/red]")
        raise typer.Exit(1)

    selected_provider, backend_url = select_llm_provider()
    deep_model = select_deep_thinking_agent(selected_provider)
    provider_lower = selected_provider.lower()
    thinking_level = reasoning_effort = anthropic_effort = None
    if provider_lower == "google":
        thinking_level = ask_gemini_thinking_config()
    elif provider_lower == "openai":
        reasoning_effort = ask_openai_reasoning_effort()
    elif provider_lower == "anthropic":
        anthropic_effort = ask_anthropic_effort()

    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = provider_lower
    config["deep_think_llm"] = deep_model
    config["quick_think_llm"] = deep_model
    config["backend_url"] = backend_url
    config["google_thinking_level"] = thinking_level
    config["openai_reasoning_effort"] = reasoning_effort
    config["anthropic_effort"] = anthropic_effort

    def _parse_brief(ticker_dir: Path) -> "dict | None":
        brief_path = ticker_dir / "earnings_brief.md"
        if not brief_path.exists():
            return None
        text = brief_path.read_text(encoding="utf-8")
        m = _re.search(r"```json\s*(\{.*?\})\s*```", text, _re.DOTALL)
        if not m:
            return None
        try:
            scores = _json.loads(m.group(1))
        except _json.JSONDecodeError:
            return None
        return {
            "ticker":        ticker_dir.name,
            "earnings_date": scores.get("earnings_date", "unknown"),
            "beat_score":    int(scores.get("beat_score", 0)),
            "guidance_score": int(scores.get("guidance_score", 0)),
            "setup_score":   int(scores.get("setup_score", 0)),
            "total_score":   int(scores.get("total_score", 0)),
            "signal":        scores.get("signal", "SKIP"),
            "confidence":    scores.get("confidence", "?"),
            "one_liner":     scores.get("one_liner", ""),
        }

    ticker_dirs = sorted(
        [d for d in screening_dir.iterdir() if d.is_dir() and (d / "earnings_brief.md").exists()]
    )
    if not ticker_dirs:
        console.print(f"[red]No ticker folders with earnings_brief.md found in {screening_dir}[/red]")
        raise typer.Exit(1)

    results = []
    with console.status("[dim]Reading ticker briefs and fetching sectors...[/dim]"):
        for td in ticker_dirs:
            r = _parse_brief(td)
            if r is None:
                console.print(f"[yellow]  Skipping {td.name} — could not parse earnings_brief.md[/yellow]")
                continue
            r["sector"] = _fetch_sector(r["ticker"])
            results.append(r)

    if not results:
        console.print("[red]No valid ticker results found.[/red]")
        raise typer.Exit(1)

    sorted_results = sorted(results, key=lambda r: r.get("total_score", 0), reverse=True)

    name_parts = screening_dir.name.split("_")
    try:
        trade_date = name_parts[1]
    except IndexError:
        from datetime import date as _date
        trade_date = str(_date.today())

    depth_label = "Rescreened"
    table_lines = [
        f"# Earnings Screener — {depth_label} — {trade_date}\n\n",
        "| # | Ticker | Sector | Earnings | Beat | Guidance | Setup | Total | Signal | Confidence | One-liner |\n",
        "|---|--------|--------|----------|------|----------|-------|-------|--------|------------|-----------|\n",
    ]
    for i, r in enumerate(sorted_results, 1):
        table_lines.append(
            f"| {i} | {r['ticker']} | {r.get('sector','Unknown')} | {r.get('earnings_date','?')} "
            f"| {r.get('beat_score',0):+d} | {r.get('guidance_score',0):+d} "
            f"| {r.get('setup_score',0):+d} | {r.get('total_score',0):+d} "
            f"| {r.get('signal','?')} | {r.get('confidence','?')} "
            f"| {r.get('one_liner','')} |\n"
        )
    (screening_dir / "screening_table.md").write_text("".join(table_lines), encoding="utf-8")
    console.print(f"[green]✓ screening_table.md rebuilt[/green] ({len(sorted_results)} tickers)\n")

    def sc(n: int) -> str:
        style = "green" if n > 0 else ("red" if n < 0 else "dim")
        return f"[{style}]{n:+d}[/{style}]"

    tbl = Table(box=box.ROUNDED, title=f"[bold]Earnings Screener — {trade_date}[/bold]", show_lines=True)
    tbl.add_column("#", justify="right", style="dim", width=3)
    tbl.add_column("Ticker", style="cyan bold", width=8)
    tbl.add_column("Sector", width=16)
    tbl.add_column("Earnings", width=12)
    tbl.add_column("Beat", justify="center", width=6)
    tbl.add_column("Guidance", justify="center", width=9)
    tbl.add_column("Setup", justify="center", width=7)
    tbl.add_column("Total", justify="center", width=7)
    tbl.add_column("Signal", justify="center", width=8)
    tbl.add_column("Conf.", justify="center", width=7)
    tbl.add_column("One-liner", no_wrap=False, min_width=30)
    for i, r in enumerate(sorted_results, 1):
        total = r.get("total_score", 0)
        signal = r.get("signal", "?")
        signal_color = {"BUY": "green", "SHORT": "red", "SKIP": "yellow"}.get(signal, "white")
        total_color = "green" if total > 0 else ("red" if total < 0 else "dim")
        tbl.add_row(
            str(i), r["ticker"], r.get("sector", "Unknown"), r.get("earnings_date", "?"),
            sc(r.get("beat_score", 0)), sc(r.get("guidance_score", 0)), sc(r.get("setup_score", 0)),
            f"[{total_color}]{total:+d}[/{total_color}]",
            f"[{signal_color}]{signal}[/{signal_color}]",
            r.get("confidence", "?"), r.get("one_liner", ""),
        )
    console.print(tbl)

    console.print()
    console.print(Rule("[bold magenta]Allocation Manager — AI Council[/bold magenta]"))
    allocation_report = None
    try:
        ta_alloc = TradingAgentsGraph(debug=False, config=config)
        alloc_layer = AllocationLayer(llm=ta_alloc.deep_thinking_llm, budget=budget, advisor_llms=build_advisor_llms(config))
        allocation_report = alloc_layer.allocate(
            results=sorted_results,
            trade_date=trade_date,
            screening_dir=screening_dir,
            save=True,
            progress_cb=lambda msg: console.print(f"  [dim]{msg}[/dim]"),
        )
    except Exception as exc:
        console.print(f"[red]Allocation Manager error: {exc}[/red]")
        raise typer.Exit(1)

    if allocation_report:
        console.print(
            Panel(
                Markdown(allocation_report),
                title=f"[bold magenta]Portfolio Allocation — ${budget:,}[/bold magenta]",
                border_style="magenta",
                padding=(1, 2),
            )
        )

        alloc_data = parse_allocation(allocation_report)
        allocations = alloc_data.get("allocations", [])
        if allocations:
            alloc_table = Table(box=box.ROUNDED, title="[bold]Allocation Summary[/bold]", show_lines=True)
            alloc_table.add_column("Ticker",    style="cyan bold", width=8)
            alloc_table.add_column("Direction", justify="center",  width=10)
            alloc_table.add_column("Amount",    justify="right",   width=12)
            alloc_table.add_column("% Budget",  justify="center",  width=9)
            alloc_table.add_column("Conviction", justify="center", width=10)
            alloc_table.add_column("Rationale", no_wrap=False, min_width=30)
            for a in allocations:
                direction = a.get("direction", "SKIP")
                dir_color = {"BUY": "green", "SHORT": "red", "SKIP": "dim"}.get(direction, "white")
                amount = a.get("amount", 0)
                alloc_table.add_row(
                    a.get("ticker", ""),
                    f"[{dir_color}]{direction}[/{dir_color}]",
                    f"[{dir_color}]{'$'+f'{amount:,}' if amount else '—'}[/{dir_color}]",
                    f"{a.get('pct_of_budget', 0):.1f}%",
                    a.get("conviction", ""),
                    a.get("rationale", ""),
                )
            deployed = alloc_data.get("total_deployed", 0)
            cash = alloc_data.get("cash_reserved", 0)
            console.print(alloc_table)
            console.print(
                f"  Deployed: [green]${deployed:,}[/green]  "
                f"Cash: [yellow]${cash:,}[/yellow]  "
                f"Long: [green]${alloc_data.get('long_exposure', 0):,}[/green]  "
                f"Short: [red]${alloc_data.get('short_exposure', 0):,}[/red]"
            )

        console.print(f"\n[green]✓ Saved to:[/green] {(screening_dir / 'allocation.md').resolve()}")


def allocation_weights(
    beat:         Optional[float] = typer.Option(None, "--beat",         help="Weight for beat score bucket"),
    guidance:     Optional[float] = typer.Option(None, "--guidance",     help="Weight for guidance score bucket"),
    setup:        Optional[float] = typer.Option(None, "--setup",        help="Weight for setup score bucket"),
    fundamentals: Optional[float] = typer.Option(None, "--fundamentals", help="Weight for fundamentals score bucket"),
    reset:        bool            = typer.Option(False, "--reset",        help="Reset all weights to defaults"),
):
    """View or update the scoring weights used by the Allocation Manager.

    Weights scale each analysis bucket when computing the weighted_score that
    the AI council uses for sizing decisions. A weight > 1.0 amplifies that
    bucket; < 1.0 dampens it. Fundamentals now contributes directly to the
    weighted_score alongside beat/guidance/setup.

    Examples:\n
      tradingagents allocation-weights                         # show current weights\n
      tradingagents allocation-weights --fundamentals 2.0      # trust fundamentals more\n
      tradingagents allocation-weights --beat 0.5 --setup 1.2\n
      tradingagents allocation-weights --reset
    """
    from tradingagents.allocation.weights import load_weights, save_weights

    if reset:
        save_weights(0.7, 1.0, 1.0, 1.5)
        console.print("[green]Weights reset to defaults: beat=0.70  guidance=1.00  setup=1.00  fundamentals=1.50[/green]")
        return

    current = load_weights()

    if beat is None and guidance is None and setup is None and fundamentals is None:
        w_tbl = Table(box=box.ROUNDED, title="[bold]Allocation Scoring Weights[/bold]", show_lines=True)
        w_tbl.add_column("Bucket",       style="cyan", width=14)
        w_tbl.add_column("Weight",       justify="right", width=8)
        w_tbl.add_column("Effect",       width=48)
        w_tbl.add_row("fundamentals", f"{current['fundamentals']:.2f}", "Business quality — balance sheet, margins, growth (highest weight)")
        w_tbl.add_row("guidance",     f"{current['guidance']:.2f}",     "Forward guidance tone confidence")
        w_tbl.add_row("setup",        f"{current['setup']:.2f}",        "Technical / fundamental pre-earnings setup")
        w_tbl.add_row("beat",         f"{current['beat']:.2f}",         "EPS beat prediction confidence (lowest weight — noisy signal)")
        console.print(w_tbl)
        console.print(
            "\n[dim]weighted_score = beat_w × beat_score + guidance_w × guidance_score "
            "+ setup_w × setup_score + fundamentals_w × fundamentals_score[/dim]"
        )
        console.print(
            "[dim]Adjust weights based on calibration results to reflect which bucket is most predictive.[/dim]\n"
        )
        return

    new_beat         = beat         if beat         is not None else current["beat"]
    new_guidance     = guidance     if guidance     is not None else current["guidance"]
    new_setup        = setup        if setup        is not None else current["setup"]
    new_fundamentals = fundamentals if fundamentals is not None else current["fundamentals"]

    for name, val in [("beat", new_beat), ("guidance", new_guidance), ("setup", new_setup), ("fundamentals", new_fundamentals)]:
        if val < 0:
            console.print(f"[red]Weight for '{name}' must be ≥ 0 (got {val}).[/red]")
            raise typer.Exit(1)

    save_weights(new_beat, new_guidance, new_setup, new_fundamentals)
    console.print(
        f"[green]Weights updated:[/green] "
        f"beat=[cyan]{new_beat:.2f}[/cyan]  "
        f"guidance=[cyan]{new_guidance:.2f}[/cyan]  "
        f"setup=[cyan]{new_setup:.2f}[/cyan]  "
        f"fundamentals=[cyan]{new_fundamentals:.2f}[/cyan]"
    )
