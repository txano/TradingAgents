"""Trade reflection command — post-mortem on a completed trade."""

import datetime
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
from tradingagents.reflection import ReflectionLayer
from tradingagents.reflection.layer import parse_reflection_score
from cli.utils import (
    select_llm_provider, select_deep_thinking_agent,
    ask_gemini_thinking_config, ask_openai_reasoning_effort, ask_anthropic_effort,
)
from cli.commands.common import _trades_path
from tradingagents.learning.trade_reflections import (
    consolidate_trades as _consolidate_for_reflect,
    find_analysis_for_ticker as _find_analysis_for_ticker,
)

console = Console()


def _trade_alloc_score(trade: dict) -> str:
    """Return the total_score from the earnings brief, or '—' if unavailable."""
    ap = trade.get("analysis_path")
    if not ap:
        return "—"
    brief = Path(ap) / "earnings_brief.md"
    if not brief.exists():
        return "—"
    import re as _re2
    m = _re2.search(r'"total_score"\s*:\s*(-?\d+)', brief.read_text(encoding="utf-8"))
    return f"{int(m.group(1)):+d}" if m else "—"


def reflect():
    """Run a post-mortem on a trade from your history.

    Shows all trades with P&L and reflection status, lets you pick one,
    then runs the post-mortem using data already in trades.json — no
    manual re-entry needed. Fills for the same ticker and day are merged.
    """
    import json as _json

    console.print()
    console.print(Panel(
        "[bold green]TradingAgents — Trade Reflection[/bold green]\n"
        "[dim]Select a trade to reflect on. Data is pre-filled from your trade history.[/dim]",
        border_style="green", padding=(1, 2),
    ))

    trade_log_path = _trades_path()
    if not trade_log_path.exists():
        console.print("[yellow]No trades found. Import trades first with 'tradingagents import-ibkr'.[/yellow]")
        return

    try:
        all_trades = _json.loads(trade_log_path.read_text(encoding="utf-8"))
    except Exception:
        all_trades = []

    if not all_trades:
        console.print("[yellow]trades.json is empty.[/yellow]")
        return

    reports_dir = Path("reports")

    all_indexed = sorted(enumerate(all_trades), key=lambda x: x[1].get("exit_date", ""), reverse=True)
    groups = _consolidate_for_reflect(all_indexed, reports_dir)

    show_mode = questionary.select(
        "Show trades:",
        choices=["Pending reflection only", "All trades"],
    ).ask()
    if show_mode is None:
        return
    if show_mode == "Pending reflection only":
        groups = [g for g in groups if not g["reflected"]]
        if not groups:
            console.print("[green]All trades already have a reflection.[/green]")
            return

    search = questionary.text("Filter by ticker (leave blank for all):").ask()
    if search is None:
        return
    if search.strip():
        q = search.strip().upper()
        groups = [g for g in groups if q in g["ticker"].upper()]
        if not groups:
            console.print(f"[yellow]No trades found matching '{q}'.[/yellow]")
            return

    total_fills = sum(g["fills"] for g in groups)
    title_str = f"[bold]Trade History — {len(groups)} trade{'s' if len(groups) != 1 else ''}"
    if total_fills != len(groups):
        title_str += f" · {total_fills} fills"
    title_str += "[/bold]"

    tbl = Table(box=box.ROUNDED, title=title_str, show_lines=True)
    tbl.add_column("#",         justify="right",  style="dim", width=4)
    tbl.add_column("Ticker",    style="cyan bold",             width=8)
    tbl.add_column("Dir",       justify="center",              width=6)
    tbl.add_column("Exit Date",                                width=12)
    tbl.add_column("P&L",       justify="right",               width=11)
    tbl.add_column("P&L %",     justify="right",               width=8)
    tbl.add_column("Outcome",   justify="center",              width=10)
    tbl.add_column("Score",     justify="center",              width=7)
    tbl.add_column("Fills",     justify="center",              width=6)
    tbl.add_column("Sector",    style="dim",                   width=14)
    tbl.add_column("Reflected", justify="center",              width=10)

    for n, g in enumerate(groups, 1):
        pnl     = g["pnl"]
        pnl_pct = g["pnl_pct"]
        outcome = g["outcome"]
        pnl_col = "green" if pnl >= 0 else "red"
        out_col = {"WIN": "green", "LOSS": "red", "BREAK_EVEN": "dim"}.get(outcome, "dim")
        dir_col = {"BUY": "green", "SHORT": "red"}.get(g["direction"], "white")
        reflected_cell = "[green]✓[/green]" if g["reflected"] else "[dim]—[/dim]"
        fills_cell = f"[dim]×{g['fills']}[/dim]" if g["fills"] > 1 else "[dim]—[/dim]"
        tbl.add_row(
            str(n),
            g["ticker"],
            f"[{dir_col}]{g['direction']}[/{dir_col}]",
            g["exit_date"],
            f"[{pnl_col}]${pnl:+.2f}[/{pnl_col}]",
            f"[{pnl_col}]{pnl_pct:+.1f}%[/{pnl_col}]",
            f"[{out_col}]{outcome}[/{out_col}]",
            _trade_alloc_score(g),
            fills_cell,
            g["sector"] or "—",
            reflected_cell,
        )

    console.print(tbl)

    raw = questionary.text(
        f"Enter number(s) 1–{len(groups)}, comma-separated, or 'q' to quit:"
    ).ask()
    if not raw or raw.strip().lower() == "q":
        return

    picks: list = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
            if 1 <= n <= len(groups):
                picks.append(n)
            else:
                console.print(f"[red]{n} is out of range (1–{len(groups)}), skipping.[/red]")
        except ValueError:
            console.print(f"[red]'{part}' is not a valid number, skipping.[/red]")

    seen: set = set()
    picks = [p for p in picks if not (p in seen or seen.add(p))]  # type: ignore[func-returns-value]

    if not picks:
        console.print("[red]No valid selections.[/red]")
        return

    selected_groups = [groups[p - 1] for p in picks]

    def qbox(title, prompt):
        return Panel(f"[bold]{title}[/bold]\n[dim]{prompt}[/dim]", border_style="blue", padding=(1, 2))

    suffix = f" ({len(selected_groups)} trades)" if len(selected_groups) > 1 else ""
    console.print(qbox("LLM Provider", f"Select the provider for the post-mortem{suffix}"))
    selected_provider, backend_url = select_llm_provider()
    console.print(qbox("Model", f"Select the model for the post-mortem{suffix}"))
    deep_model = select_deep_thinking_agent(selected_provider)

    thinking_level = reasoning_effort = anthropic_effort = None
    provider_lower = selected_provider.lower()
    if provider_lower == "google":
        thinking_level = ask_gemini_thinking_config()
    elif provider_lower == "openai":
        reasoning_effort = ask_openai_reasoning_effort()
    elif provider_lower == "anthropic":
        anthropic_effort = ask_anthropic_effort()

    config = DEFAULT_CONFIG.copy()
    config["llm_provider"]            = provider_lower
    config["deep_think_llm"]          = deep_model
    config["quick_think_llm"]         = deep_model
    config["backend_url"]             = backend_url
    config["google_thinking_level"]   = thinking_level
    config["openai_reasoning_effort"] = reasoning_effort
    config["anthropic_effort"]        = anthropic_effort

    try:
        ta    = TradingAgentsGraph(debug=False, config=config)
        layer = ReflectionLayer(llm=ta.deep_thinking_llm)
    except Exception as exc:
        console.print(f"[red]Failed to initialise LLM: {exc}[/red]")
        return

    batch_results: list = []

    for batch_n, group in enumerate(selected_groups, 1):
        if len(selected_groups) > 1:
            console.print()
            console.print(Rule(f"[dim]Trade {batch_n} of {len(selected_groups)}[/dim]"))

        ticker       = group["ticker"]
        direction    = group["direction"]
        shares       = group["shares"]
        entry_price  = group["entry_price"]
        exit_price   = group["exit_price"]
        pnl          = group["pnl"]
        pnl_pct      = group["pnl_pct"]
        exit_date    = group["exit_date"]
        trade_date   = group.get("trade_date") or exit_date
        orig_indices = group["orig_indices"]

        pnl_col = "green" if pnl >= 0 else "red"
        dir_col = {"BUY": "green", "SHORT": "red"}.get(direction, "white")
        fills_note = f"  Fills:     {group['fills']} partial orders\n" if group["fills"] > 1 else ""
        console.print()
        console.print(Panel(
            f"  Ticker:    [cyan bold]{ticker}[/cyan bold]\n"
            f"  Direction: [{dir_col}]{direction}[/{dir_col}]\n"
            f"  Shares:    {shares:,.0f}\n"
            f"{fills_note}"
            f"  Entry:    ${entry_price:.2f}  →  Exit: ${exit_price:.2f}\n"
            f"  P&L:      [{pnl_col}]${pnl:+.2f} ({pnl_pct:+.1f}%)[/{pnl_col}]\n"
            f"  Exit date: {exit_date}",
            title=f"[bold]Reflecting on: {ticker}[/bold]",
            border_style="cyan", padding=(1, 2),
        ))

        if group["reflected"]:
            existing = group["existing_reflection_path"]
            console.print(f"[yellow]A reflection already exists:[/yellow] [dim]{existing}[/dim]")
            if not questionary.confirm("Overwrite?", default=False).ask():
                batch_results.append((ticker, exit_date, "skipped", None))
                continue

        analysis_path = None
        stored_ap = group.get("analysis_path")
        if stored_ap and Path(stored_ap).exists():
            analysis_path = Path(stored_ap)
            console.print(f"\n[dim]Using stored analysis: {analysis_path.name}[/dim]")
        else:
            analyses = _find_analysis_for_ticker(reports_dir, ticker)
            if analyses:
                console.print(f"\n[green]Found {len(analyses)} analysis folder(s) for {ticker}:[/green]")
                for i, p in enumerate(analyses, 1):
                    tag = " [dim](earnings brief)[/dim]" if (p / "earnings_brief.md").exists() else ""
                    console.print(f"  [cyan]{i}.[/cyan] {p.name}{tag}")
                console.print("  [dim]0. Skip[/dim]")
                while True:
                    c = typer.prompt("Select analysis", default="1").strip()
                    try:
                        n = int(c)
                        if n == 0:
                            break
                        if 1 <= n <= len(analyses):
                            analysis_path = analyses[n - 1]
                            break
                        console.print(f"[red]Enter 0–{len(analyses)}[/red]")
                    except ValueError:
                        console.print("[red]Enter a number[/red]")
            else:
                console.print(f"\n[yellow]No prior analysis found for {ticker}. Proceeding without it.[/yellow]")

        console.print()
        console.print(Rule(f"[bold cyan]Post-Mortem: {ticker} ({direction})[/bold cyan]"))
        console.print()

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        save_dir  = Path("reports") / "reflections" / f"{ticker}_{exit_date}_{timestamp}"

        post_mortem = None
        with console.status(f"[bold yellow]Generating post-mortem for {ticker}...[/bold yellow]"):
            try:
                post_mortem = layer.analyze(
                    ticker=ticker,
                    trade_date=trade_date,
                    exit_date=exit_date,
                    direction=direction,
                    shares=shares,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    prior_analysis_path=str(analysis_path) if analysis_path else None,
                    save_dir=str(save_dir),
                )
            except Exception as exc:
                console.print(f"[red]Reflection error for {ticker}: {exc}[/red]")
                batch_results.append((ticker, exit_date, "error", None))
                continue

        if post_mortem:
            console.print(Panel(
                Markdown(post_mortem),
                title=f"[bold yellow]Trade Post-Mortem: {ticker}[/bold yellow]",
                border_style="yellow", padding=(1, 2),
            ))

        score = parse_reflection_score(post_mortem) if post_mortem else {}

        screening_run = None
        if analysis_path:
            for parent in Path(analysis_path).parents:
                if parent.name.startswith("screening_"):
                    screening_run = str(parent)
                    break
        if not screening_run:
            for idx in orig_indices:
                sr = all_trades[idx].get("screening_run")
                if sr:
                    screening_run = sr
                    break

        update = {
            "beat_prediction_correct":     score.get("beat_prediction_correct"),
            "guidance_prediction_correct": score.get("guidance_prediction_correct"),
            "key_lesson":                  score.get("key_lesson", ""),
            "outcome":                     score.get("outcome") or group["outcome"],
            "prediction_accuracy":         score.get("prediction_accuracy", "UNKNOWN"),
            "screening_run":               screening_run,
            "analysis_path":               str(analysis_path) if analysis_path else group.get("analysis_path"),
            "reflection_path":             str(save_dir),
            "reflected_at":                datetime.datetime.now().isoformat(),
        }
        for idx in orig_indices:
            all_trades[idx].update(update)

        trade_log_path.write_text(_json.dumps(all_trades, indent=2), encoding="utf-8")

        console.print(f"\n[green]✓ Saved:[/green] {save_dir.resolve()}")
        if len(orig_indices) > 1:
            console.print(f"[dim]  ({len(orig_indices)} fill records updated)[/dim]")

        batch_results.append((ticker, exit_date, "done", save_dir))

    if len(selected_groups) > 1:
        console.print()
        console.print(Rule("[bold]Reflection Summary[/bold]"))
        for t, d, status, path in batch_results:
            if status == "done":
                console.print(f"  [green]✓[/green]  {t}  {d}")
            elif status == "skipped":
                console.print(f"  [yellow]—[/yellow]  {t}  {d}  [dim]skipped[/dim]")
            else:
                console.print(f"  [red]✗[/red]  {t}  {d}  [dim]error[/dim]")
        done = sum(1 for _, _, s, _ in batch_results if s == "done")
        console.print(f"\n[dim]{done} of {len(batch_results)} completed · trades.json updated[/dim]")

    from cli.commands.reports import _auto_build_web
    _auto_build_web()
