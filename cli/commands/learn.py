"""`learn` — automated reflect-all + self-improvement loop.

Non-interactive end-to-end pass:
  1. Reflect on every trade in trades.json (pending only; --force redoes all).
  2. Analyse all reflections with one deep-think LLM call.
  3. Auto-apply the recommended scoring weights and (guarded) prompt-source edits.

Uses the default configured model — no per-trade prompts — so it is safe to run
on a schedule. This is the seed of ROADMAP #13 ("dream mode").
"""

import datetime
import json as _json
import os
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.llm_clients.api_key_env import get_api_key_env
from tradingagents.reflection import ReflectionLayer
from tradingagents.learning.trade_reflections import reflect_all
from tradingagents.learning.self_improve import (
    apply_proposal as apply_proposal_fn,
    run_self_improvement,
    scan_reflection_items,
)
from cli.commands.common import _trades_path
from cli.utils import ensure_api_key, select_deep_thinking_agent, select_llm_provider

console = Console()

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _print_summary(summary: dict, dry_run: bool) -> None:
    """Render the apply summary panel + file pointers (shared by both paths)."""
    wc = summary["weight_changes"]
    applied = summary["prompt_applied"]
    rejected = summary["prompt_rejected"]
    verb = "Would change" if dry_run else "Changed"
    body = [f"[bold]{verb}:[/bold]", "", f"  Scoring weights: {len(wc)}"]
    body += [f"    • {c}" for c in wc]
    body.append(f"  Prompt edits applied: {len(applied)}")
    body += [f"    • {c}" for c in applied]
    if rejected:
        body.append(f"  [yellow]Prompt edits rejected by safety harness: {len(rejected)}[/yellow]")
        body += [f"    • {c}" for c in rejected]
    if summary["process_notes"]:
        body.append(f"  Process notes (not auto-applied): {len(summary['process_notes'])}")

    console.print()
    console.print(Panel("\n".join(body), title="[bold]Self-Improvement Summary[/bold]",
                        border_style="green" if not dry_run else "yellow", padding=(1, 2)))
    if summary.get("report_path"):
        console.print(f"[green]✓ Report:[/green]    {summary['report_path'].resolve()}")
    console.print(f"[green]✓ Changelog:[/green] {summary['changelog_path'].resolve()}")
    if applied and not dry_run:
        console.print("[dim]  Prompt-source files were edited (originals backed up in the run folder). "
                      "Review with `git diff` and revert if needed.[/dim]")


def _auto_build_web_safe() -> None:
    try:
        from cli.commands.reports import _auto_build_web
        _auto_build_web()
    except Exception:
        pass


def learn(
    provider: Optional[str] = typer.Option(
        None, "--provider", help="LLM provider (e.g. deepseek, openai, anthropic). "
        "Omit (with --model) to be prompted once interactively."
    ),
    model: Optional[str] = typer.Option(
        None, "--model", help="Model name (e.g. deepseek-v4-pro). "
        "Pass together with --provider to run non-interactively; omit to be prompted."
    ),
    apply_proposal_path: Optional[str] = typer.Option(
        None, "--apply-proposal", help="Apply a previously reviewed proposal.json verbatim "
        "(no reflection, no LLM call). e.g. reports/self_improve_TIMESTAMP/proposal.json"
    ),
    force: bool = typer.Option(
        False, "--force", help="Re-run reflections for ALL trades, overwriting existing ones."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Analyse and report, but apply no weight or prompt changes."
    ),
    skip_reflect: bool = typer.Option(
        False, "--skip-reflect", help="Skip the reflection pass; only analyse existing reflections."
    ),
    no_weights: bool = typer.Option(
        False, "--no-weights", help="Do not auto-apply scoring-weight changes."
    ),
    no_prompt_edits: bool = typer.Option(
        False, "--no-prompt-edits", help="Do not auto-apply prompt-source edits."
    ),
) -> None:
    """Reflect on every trade, then analyse the lot and apply improvements automatically."""
    # Ensure .env keys are in os.environ even if this is reached outside the
    # normal CLI entry point (e.g. tests, scheduled invocation).
    load_dotenv()
    load_dotenv(".env.enterprise", override=False)

    console.print()
    console.print(Panel(
        "[bold green]TradingAgents — Learn (automated self-improvement)[/bold green]\n"
        "[dim]Reflect on all trades → analyse → apply weight & prompt improvements.[/dim]",
        border_style="green", padding=(1, 2),
    ))
    if dry_run:
        console.print("[yellow]DRY RUN — nothing will be applied.[/yellow]")

    # ---- Apply a previously reviewed proposal.json verbatim (no LLM) ----------
    if apply_proposal_path:
        ppath = Path(apply_proposal_path)
        if not ppath.exists():
            console.print(f"[red]Proposal file not found: {ppath}[/red]")
            return
        try:
            proposal = _json.loads(ppath.read_text(encoding="utf-8"))
        except Exception as exc:
            console.print(f"[red]Could not parse {ppath}: {exc}[/red]")
            return
        console.print(Rule("[bold cyan]Applying reviewed proposal[/bold cyan]"))
        console.print(f"[dim]From: {ppath}[/dim]")
        summary = apply_proposal_fn(
            proposal, _REPO_ROOT, ppath.parent,
            apply_weights=not no_weights,
            apply_prompts=not no_prompt_edits,
            dry_run=dry_run,
            progress_cb=lambda m: console.print(f"[dim]{m}[/dim]"),
        )
        _print_summary(summary, dry_run)
        _auto_build_web_safe()
        return

    trade_log_path = _trades_path()
    if not trade_log_path.exists():
        console.print("[yellow]No trades found. Import trades first with 'tradingagents import-ibkr'.[/yellow]")
        return
    try:
        all_trades = _json.loads(trade_log_path.read_text(encoding="utf-8"))
    except Exception as exc:
        console.print(f"[red]Could not read {trade_log_path}: {exc}[/red]")
        return
    if not all_trades:
        console.print("[yellow]trades.json is empty.[/yellow]")
        return

    reports_dir = Path("reports")

    # ---- LLM provider + model -------------------------------------------------
    # If both are passed as flags, run fully non-interactively (scheduler use).
    # Otherwise prompt ONCE up front, then run the whole batch with that choice.
    config = DEFAULT_CONFIG.copy()
    if provider and model:
        config["llm_provider"] = provider.lower()
        config["deep_think_llm"] = config["quick_think_llm"] = model
        config["backend_url"] = None  # let the client resolve the provider's base URL
        env_var = get_api_key_env(config["llm_provider"])
        if env_var and not os.environ.get(env_var):
            console.print(
                f"[red]{env_var} is not set.[/red] Add it to your .env "
                f"(e.g. [cyan]{env_var}=...[/cyan]) or pick a provider whose key is configured."
            )
            return
    else:
        sel_provider, backend_url = select_llm_provider()
        ensure_api_key(sel_provider)
        sel_model = select_deep_thinking_agent(sel_provider)
        config["llm_provider"] = sel_provider
        config["deep_think_llm"] = config["quick_think_llm"] = sel_model
        config["backend_url"] = backend_url

    try:
        ta = TradingAgentsGraph(debug=False, config=config)
        llm = ta.deep_thinking_llm
    except Exception as exc:
        console.print(f"[red]Failed to initialise LLM: {exc}[/red]")
        return
    console.print(f"[dim]Model: {config['llm_provider']}:{config['deep_think_llm']}[/dim]")

    # ---- 1. Reflect on all trades --------------------------------------------
    if not skip_reflect:
        console.print()
        console.print(Rule("[bold cyan]Reflecting on trades[/bold cyan]"))
        layer = ReflectionLayer(llm=llm)
        updated_trades, results = reflect_all(
            layer, all_trades, reports_dir, force=force, progress_cb=console.print,
        )
        if not dry_run:
            trade_log_path.write_text(_json.dumps(updated_trades, indent=2), encoding="utf-8")
        done = sum(1 for r in results if r["status"] == "done")
        errors = sum(1 for r in results if r["status"] == "error")
        console.print(f"[green]Reflected {done} trade(s)[/green]"
                      + (f", [red]{errors} error(s)[/red]" if errors else "")
                      + ("  [dim](dry run: trades.json not written)[/dim]" if dry_run else ""))

    # ---- 2 + 3. Analyse and apply --------------------------------------------
    items = scan_reflection_items(reports_dir, all_trades)
    if not items:
        console.print("[yellow]No reflections to analyse. Nothing to improve.[/yellow]")
        return

    console.print()
    console.print(Rule("[bold cyan]Analysing reflections & applying improvements[/bold cyan]"))
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = reports_dir / f"self_improve_{timestamp}"

    with console.status("[bold yellow]Generating improvement report and applying changes…[/bold yellow]"):
        try:
            summary = run_self_improvement(
                llm, items, _REPO_ROOT, run_dir,
                apply_weights=not no_weights,
                apply_prompts=not no_prompt_edits,
                dry_run=dry_run,
                progress_cb=lambda m: console.print(f"[dim]{m}[/dim]"),
            )
        except Exception as exc:
            console.print(f"[red]Self-improvement analysis failed: {exc}[/red]")
            return

    _print_summary(summary, dry_run)
    _auto_build_web_safe()
