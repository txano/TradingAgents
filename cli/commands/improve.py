"""System improvement analysis command."""

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
from tradingagents.reflection.layer import parse_reflection_score
from cli.utils import (
    select_llm_provider, select_deep_thinking_agent,
    ask_gemini_thinking_config, ask_openai_reasoning_effort, ask_anthropic_effort,
)

console = Console()


def _scan_reflection_folders(reports_dir: Path, all_trades: list) -> list:
    """Return one entry per unique ticker+exit_date (most-recent run wins for duplicates)."""
    reflections_dir = reports_dir / "reflections"
    if not reflections_dir.exists():
        return []

    trade_lookup: dict = {}
    for t in all_trades:
        key = (t.get("ticker", ""), t.get("exit_date", ""))
        prev = trade_lookup.get(key)
        if prev is None or (t.get("reflected_at", "") or "") > (prev.get("reflected_at", "") or ""):
            trade_lookup[key] = t

    folders: list = []
    for d in reflections_dir.iterdir():
        if not d.is_dir():
            continue
        pm = d / "post_mortem.md"
        if not pm.exists():
            continue
        parts = d.name.split("_")
        if len(parts) < 3:
            continue
        ticker    = parts[0]
        exit_date = parts[1]
        timestamp = "_".join(parts[2:])
        folders.append((timestamp, d, ticker, exit_date))
    folders.sort(key=lambda x: x[0], reverse=True)

    seen: set = set()
    items: list = []
    for timestamp, d, ticker, exit_date in folders:
        key = (ticker, exit_date)
        if key in seen:
            continue
        seen.add(key)
        content = (d / "post_mortem.md").read_text(encoding="utf-8")
        score   = parse_reflection_score(content)
        trade   = trade_lookup.get(key, {})
        items.append({
            "ticker":        ticker,
            "exit_date":     exit_date,
            "timestamp":     timestamp,
            "direction":     score.get("direction") or trade.get("direction", "?"),
            "pnl":           trade.get("pnl"),
            "pnl_pct":       score.get("pnl_pct") if score.get("pnl_pct") is not None else trade.get("pnl_pct"),
            "outcome":       score.get("outcome") or trade.get("outcome", "?"),
            "beat_correct":  score.get("beat_prediction_correct"),
            "guide_correct": score.get("guidance_prediction_correct"),
            "key_lesson":    score.get("key_lesson", ""),
            "content":       content,
            "folder":        d,
        })

    items.sort(key=lambda x: x["exit_date"], reverse=True)
    return items


def _build_improve_prompt(items: list) -> str:
    """Build the structured LLM prompt from a list of reflection items."""
    n = len(items)
    wins   = sum(1 for i in items if i["outcome"] == "WIN")
    losses = sum(1 for i in items if i["outcome"] == "LOSS")

    beat_all   = [i for i in items if i["beat_correct"]  is not None]
    guide_all  = [i for i in items if i["guide_correct"] is not None]
    beat_acc   = f"{sum(1 for i in beat_all  if i['beat_correct'])}/{len(beat_all)}"   if beat_all  else "N/A"
    guide_acc  = f"{sum(1 for i in guide_all if i['guide_correct'])}/{len(guide_all)}" if guide_all else "N/A"

    pnl_pcts  = [i["pnl_pct"] for i in items if i["pnl_pct"] is not None]
    avg_pct   = sum(pnl_pcts) / len(pnl_pcts) if pnl_pcts else 0.0

    lines = [
        "# Trade Reflection Analysis — System Improvement Request",
        "",
        "## Context: What TradingAgents Does",
        "",
        "TradingAgents is a pre-earnings research and automated allocation framework:",
        "- **Pipeline**: 5 LangGraph teams in sequence — market analyst, fundamentals analyst,",
        "  news analyst, social analyst → bull/bear researchers → research manager →",
        "  risk management (aggressive/conservative/neutral) → portfolio manager (BUY/SHORT/SKIP)",
        "- **Scoring**: each ticker receives four scores (-5 to +5):",
        "  - `beat_score`: EPS beat likelihood",
        "  - `guidance_score`: forward guidance tone",
        "  - `setup_score`: technical/fundamental pre-earnings setup",
        "  - `fundamentals_score`: business quality, grounded in real statement metrics",
        "  - `weighted_score = beat_w × beat + guidance_w × guidance + setup_w × setup + fundamentals_w × fundamentals`",
        "- **Allocation**: an AI Council (5 persona advisors → cross-review → synthesis) sizes",
        "  positions using the weighted_score, then a deterministic validator enforces position/",
        "  sector caps and budget arithmetic; high conviction = 15–25%, medium = 7–14%, low ≤ 6% or SKIP",
        "",
        "## Batch Statistics",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Reflections | {n} |",
        f"| Win / Loss | {wins} / {losses} ({wins/n*100:.0f}% win rate) |",
        f"| Avg P&L % | {avg_pct:+.1f}% |",
        f"| Beat prediction accuracy | {beat_acc} |",
        f"| Guidance prediction accuracy | {guide_acc} |",
        "",
        "## Individual Post-Mortems",
        "",
    ]

    for idx, item in enumerate(items, 1):
        outcome_tag = f"{item['direction']}, {item['outcome']}"
        lines.append(f"### {idx}. {item['ticker']} — {item['exit_date']} ({outcome_tag})")
        lines.append("")
        lines.append(item["content"].strip())
        lines.append("")
        lines.append("---")
        lines.append("")

    lines += [
        "## What I Need From You",
        "",
        "Analyse the post-mortems above and produce a structured improvement report.",
        "Be **specific and actionable**. Reference individual trades where relevant",
        "(e.g. 'As in the ANET post-mortem, ...'). Format your output in markdown.",
        "",
        "### 1. Failure Patterns",
        "The 3–5 most common failure modes. For each: what went wrong, which part of the",
        "pipeline caused it (analyst prompt / scorer / council / risk mgmt / PM), and how",
        "often it appeared across the batch.",
        "",
        "### 2. Success Patterns",
        "What the system got right when it worked. Which signals or reasoning steps were",
        "most reliable predictors of a winning trade.",
        "",
        "### 3. Missing Data / Blind Spots",
        "Information that was absent from the pre-earnings brief but would have changed",
        "the outcome. For each gap, name the specific data source or calculation to add.",
        "",
        "### 4. Prompt Improvements",
        "For each relevant agent role, a specific change. Use this format:",
        "",
        "**[Role]**",
        "- Current weakness: ...",
        "- Proposed change: ...",
        "- Expected impact: ...",
        "",
        "Roles to address: Market Analyst, Fundamentals Analyst, Bull/Bear Researchers,",
        "Research Manager, Portfolio Manager, AI Council synthesis prompt.",
        "",
        "### 5. Scoring Weight Adjustments",
        "Based on the beat/guidance accuracy statistics and the per-trade post-mortems,",
        "recommend specific numeric weights for beat_w, guidance_w, setup_w, fundamentals_w.",
        "Show your reasoning (e.g. guidance was correct only 2/8 times → reduce guidance_w).",
        "",
        "### 6. Process / Structural Changes",
        "Any new pipeline steps, new agents, threshold changes, or workflow adjustments",
        "that would structurally improve outcomes — beyond prompt tweaks.",
    ]

    return "\n".join(lines)


def improve():
    """Analyse trade reflections with an LLM and generate system-improvement suggestions.

    Scans all post-mortems, lets you choose which to include, submits them
    to the chosen LLM, and saves the output to reports/improvement_TIMESTAMP.md.
    """
    import json as _json
    from langchain_core.messages import HumanMessage, SystemMessage

    console.print()
    console.print(Panel(
        "[bold green]TradingAgents — System Improvement Analysis[/bold green]\n"
        "[dim]Synthesise trade reflections into actionable pipeline improvements.[/dim]",
        border_style="green", padding=(1, 2),
    ))

    reports_dir    = Path("reports")
    from cli.commands.common import _trades_path
    trade_log_path = _trades_path()
    all_trades: list = []
    if trade_log_path.exists():
        try:
            all_trades = _json.loads(trade_log_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    items = _scan_reflection_folders(reports_dir, all_trades)
    if not items:
        console.print("[yellow]No reflections found in reports/reflections/.[/yellow]")
        return

    tbl = Table(
        box=box.ROUNDED,
        title=f"[bold]Available Reflections — {len(items)} trade{'s' if len(items) != 1 else ''}[/bold]",
        show_lines=True,
    )
    tbl.add_column("#",          justify="right", style="dim",  width=4)
    tbl.add_column("Ticker",     style="cyan bold",             width=8)
    tbl.add_column("Exit Date",                                 width=12)
    tbl.add_column("Dir",        justify="center",              width=6)
    tbl.add_column("P&L %",      justify="right",               width=8)
    tbl.add_column("Outcome",    justify="center",              width=10)
    tbl.add_column("Beat ✓",     justify="center",              width=8)
    tbl.add_column("Guide ✓",    justify="center",              width=8)
    tbl.add_column("Key Lesson", style="dim",                   width=42)

    def _bool_cell(v: "bool | None") -> str:
        if v is True:  return "[green]✓[/green]"
        if v is False: return "[red]✗[/red]"
        return "[dim]—[/dim]"

    for n, item in enumerate(items, 1):
        pp      = item["pnl_pct"]
        outcome = item["outcome"]
        pnl_col = "green" if (pp or 0) >= 0 else "red"
        out_col = {"WIN": "green", "LOSS": "red", "BREAK_EVEN": "dim"}.get(outcome, "dim")
        dir_col = {"BUY": "green", "SHORT": "red"}.get(item["direction"], "white")
        lesson  = (item["key_lesson"] or "")
        lesson  = lesson[:55] + ("…" if len(lesson) > 55 else "")
        tbl.add_row(
            str(n),
            item["ticker"],
            item["exit_date"],
            f"[{dir_col}]{item['direction']}[/{dir_col}]",
            f"[{pnl_col}]{pp:+.1f}%[/{pnl_col}]" if pp is not None else "—",
            f"[{out_col}]{outcome}[/{out_col}]",
            _bool_cell(item["beat_correct"]),
            _bool_cell(item["guide_correct"]),
            lesson,
        )

    console.print(tbl)

    raw = questionary.text(
        f"Enter 'all', comma-separated numbers (1–{len(items)}), or 'q' to quit:"
    ).ask()
    if not raw or raw.strip().lower() == "q":
        return

    if raw.strip().lower() == "all":
        selected = list(items)
    else:
        picks: list = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                n = int(part)
                if 1 <= n <= len(items):
                    picks.append(n)
                else:
                    console.print(f"[red]{n} out of range, skipping.[/red]")
            except ValueError:
                console.print(f"[red]'{part}' is not a valid number, skipping.[/red]")
        seen_picks: set = set()
        picks = [p for p in picks if not (p in seen_picks or seen_picks.add(p))]  # type: ignore[func-returns-value]
        if not picks:
            console.print("[red]No valid selections.[/red]")
            return
        selected = [items[p - 1] for p in picks]

    console.print(f"\n[dim]Using {len(selected)} reflection(s) for analysis.[/dim]")

    def qbox(title, prompt):
        return Panel(f"[bold]{title}[/bold]\n[dim]{prompt}[/dim]", border_style="blue", padding=(1, 2))

    console.print(qbox("LLM Provider", "Select the provider for the improvement analysis"))
    selected_provider, backend_url = select_llm_provider()
    console.print(qbox("Model", "Select the model for the improvement analysis"))
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

    prompt_text = _build_improve_prompt(selected)

    console.print()
    console.print(Rule("[bold cyan]Generating System Improvement Report[/bold cyan]"))
    console.print()

    output: "str | None" = None
    with console.status("[bold yellow]Analysing reflections and generating recommendations…[/bold yellow]"):
        try:
            ta  = TradingAgentsGraph(debug=False, config=config)
            llm = ta.deep_thinking_llm
            messages = [
                SystemMessage(content=(
                    "You are a systematic trading pipeline improvement expert. "
                    "Your task is to analyse a set of trade post-mortems and produce "
                    "specific, actionable recommendations to improve the analysis pipeline, "
                    "prompts, data inputs, and scoring weights. "
                    "Be concrete: cite specific trades, propose exact prompt wording where helpful, "
                    "and give numeric weight recommendations with justification."
                )),
                HumanMessage(content=prompt_text),
            ]
            response = llm.invoke(messages)
            output   = response.content if hasattr(response, "content") else str(response)
        except Exception as exc:
            console.print(f"[red]LLM error: {exc}[/red]")
            return

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    save_path = reports_dir / f"improvement_{timestamp}.md"
    reports_dir.mkdir(parents=True, exist_ok=True)
    save_path.write_text(output, encoding="utf-8")

    console.print(Panel(
        Markdown(output),
        title="[bold yellow]System Improvement Report[/bold yellow]",
        border_style="yellow", padding=(1, 2),
    ))
    console.print(f"\n[green]✓ Report saved to:[/green] {save_path.resolve()}")
    console.print(f"[dim]  Based on {len(selected)} reflection(s) — bring this file to Claude Code to apply the changes.[/dim]")
