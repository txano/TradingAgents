"""Batch earnings screener — run_screening and screen command."""

import datetime
import json
import threading
from pathlib import Path

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
from tradingagents.earnings import EarningsLayer
from tradingagents.allocation import AllocationLayer
from tradingagents.allocation.layer import build_advisor_llms, parse_allocation
from tradingagents.allocation.pricing import fetch_pricing_context
from tradingagents.allocation.asymmetry import build_asymmetry
from tradingagents.allocation.crowding import fetch_crowding
from cli.utils import (
    select_llm_provider, select_shallow_thinking_agent, select_deep_thinking_agent,
    select_research_depth, ask_gemini_thinking_config, ask_openai_reasoning_effort,
    ask_anthropic_effort, get_analysis_date,
)
from cli.commands.common import _fetch_sector, save_report_to_disk

console = Console()


def run_screening(budget: int = 100_000, tickers_prefill: "list[str] | None" = None) -> None:
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = min(4096, hard)
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except Exception:
        pass

    console.print()
    console.print(Panel(
        "[bold green]TradingAgents Earnings Screener[/bold green]\n"
        "[dim]Batch analysis + Earnings Layer across a list of tickers[/dim]",
        border_style="green", padding=(1, 2),
    ))

    def question_box(title, prompt):
        return Panel(f"[bold]{title}[/bold]\n[dim]{prompt}[/dim]", border_style="blue", padding=(1, 2))

    if tickers_prefill:
        tickers = tickers_prefill
        console.print(f"[green]Tickers (from earnings calendar):[/green] {', '.join(tickers)}\n")
    else:
        console.print(question_box("Step 1: Tickers", "Enter tickers separated by commas (e.g. AAPL, MSFT, NVDA)"))
        raw = typer.prompt("")
        tickers = [t.strip().upper() for t in raw.split(",") if t.strip()]
        if not tickers:
            console.print("[red]No tickers entered. Exiting.[/red]")
            return
        console.print(f"[green]Tickers:[/green] {', '.join(tickers)}\n")

    default_date = datetime.datetime.now().strftime("%Y-%m-%d")
    console.print(question_box("Step 2: Analysis Date", f"Enter the analysis date (YYYY-MM-DD), default {default_date}"))
    trade_date = get_analysis_date()

    console.print(question_box("Step 3: Research Depth", "Select how deep to run the analysis for each ticker"))
    research_depth = select_research_depth()
    depth_label = {1: "Shallow", 3: "Medium", 5: "Deep"}.get(research_depth, str(research_depth))
    console.print(f"[green]Depth:[/green] {depth_label} ({research_depth} debate round(s))\n")

    console.print(question_box("Step 4: LLM Provider", "Select your LLM provider"))
    selected_provider, backend_url = select_llm_provider()

    console.print(question_box("Step 5: Quick Thinking Model", "Used for analysts and debate agents"))
    quick_model = select_shallow_thinking_agent(selected_provider)
    console.print(question_box("Step 5: Deep Thinking Model", "Used for the Earnings Layer"))
    deep_model = select_deep_thinking_agent(selected_provider)

    thinking_level = reasoning_effort = anthropic_effort = None
    provider_lower = selected_provider.lower()
    if provider_lower == "google":
        console.print(question_box("Step 6: Thinking Mode", "Configure Gemini thinking mode"))
        thinking_level = ask_gemini_thinking_config()
    elif provider_lower == "openai":
        console.print(question_box("Step 6: Reasoning Effort", "Configure OpenAI reasoning effort level"))
        reasoning_effort = ask_openai_reasoning_effort()
    elif provider_lower == "anthropic":
        console.print(question_box("Step 6: Effort Level", "Configure Claude effort level"))
        anthropic_effort = ask_anthropic_effort()

    config = DEFAULT_CONFIG.copy()
    config["llm_provider"]            = provider_lower
    config["deep_think_llm"]          = deep_model
    config["quick_think_llm"]         = quick_model
    config["backend_url"]             = backend_url
    config["max_debate_rounds"]       = research_depth
    config["max_risk_discuss_rounds"] = research_depth
    config["google_thinking_level"]   = thinking_level
    config["openai_reasoning_effort"] = reasoning_effort
    config["anthropic_effort"]        = anthropic_effort

    import os
    _PROVIDER_KEY_ENV = {
        "deepseek":   "DEEPSEEK_API_KEY",
        "xai":        "XAI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "qwen":       "DASHSCOPE_API_KEY",
        "glm":        "ZHIPU_API_KEY",
        "openai":     "OPENAI_API_KEY",
        "anthropic":  "ANTHROPIC_API_KEY",
    }
    _base_env = _PROVIDER_KEY_ENV.get(provider_lower, "")
    _api_keys: list[str] = []
    if _base_env:
        for _suffix in ["", "_2", "_3", "_4", "_5", "_6", "_7", "_8"]:
            _k = os.environ.get(_base_env + _suffix, "").strip()
            if _k and _k not in _api_keys:
                _api_keys.append(_k)

    # Resume: check for an existing screening folder for this date
    screening_dir = None
    completed_tickers: set[str] = set()
    existing_runs = sorted(
        Path("reports").glob(f"screening_{trade_date}_*/"),
        key=lambda p: p.name,
        reverse=True,
    )
    if existing_runs:
        console.print(f"\n[yellow]Found {len(existing_runs)} existing screening run(s) for {trade_date}:[/yellow]")
        for i, p in enumerate(existing_runs[:5], 1):
            done = [d.name for d in p.iterdir() if d.is_dir() and (d / "complete_report.md").exists()]
            console.print(f"  [cyan]{i}.[/cyan] {p.name}  [dim]({len(done)} completed: {', '.join(done) or 'none'})[/dim]")
        console.print("  [dim]0. Start a fresh run[/dim]")
        while True:
            choice = typer.prompt("\nResume an existing run? (enter number or 0 for fresh)", default="1").strip()
            try:
                n = int(choice)
                if n == 0:
                    break
                elif 1 <= n <= len(existing_runs[:5]):
                    screening_dir = existing_runs[n - 1]
                    completed_tickers = {
                        d.name for d in screening_dir.iterdir()
                        if d.is_dir() and (d / "complete_report.md").exists()
                    }
                    console.print(f"\n[green]Resuming:[/green] {screening_dir.name}")
                    if completed_tickers:
                        console.print(f"[dim]Skipping already completed: {', '.join(sorted(completed_tickers))}[/dim]")
                    break
                console.print(f"[red]Enter 0–{min(5, len(existing_runs))}[/red]")
            except ValueError:
                console.print("[red]Enter a number[/red]")

    if screening_dir is None:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        screening_dir = Path("reports") / f"screening_{trade_date}_{timestamp}"

    screening_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"[dim]Saving to:[/dim] {screening_dir.resolve()}\n")

    tickers_to_run = [t for t in tickers if t not in completed_tickers]
    if not tickers_to_run:
        console.print("[green]All tickers already completed. Nothing to do.[/green]")
        return

    # Parallelism picker
    num_workers = 1
    if len(tickers_to_run) > 1:
        _n_keys = len(_api_keys)
        _step_hint = (
            f"Found {_n_keys} {provider_lower.upper()} API keys — each worker uses a separate key"
            if _n_keys > 1 else
            "Run multiple tickers at once using concurrent API requests"
        )
        console.print(question_box("Step 7: Parallelism", _step_hint))
        _max_w = min(max(_n_keys, 1), 8, len(tickers_to_run))
        _par_choices = []
        for _w in range(1, _max_w + 1):
            if _w == 1:
                _label = "  1   sequential"
            elif _n_keys >= _w:
                _label = f"  {_w}   parallel  ({_w} separate API keys)"
            else:
                _label = f"  {_w}   parallel  (concurrent requests, same key)"
            _par_choices.append(questionary.Choice(_label, value=_w))
        for _w in range(_max_w + 1, 4):
            if _w <= len(tickers_to_run):
                _par_choices.append(questionary.Choice(
                    f"  {_w}   parallel  (concurrent requests, same key)", value=_w
                ))
        _default_w = min(_n_keys if _n_keys > 1 else 1, len(_par_choices))
        num_workers = questionary.select(
            "Workers:",
            choices=_par_choices,
            default=_par_choices[_default_w - 1],
        ).ask() or 1
        console.print(f"[green]Workers:[/green] {num_workers}\n")

    console.print(Rule(
        f"[bold cyan]Running {len(tickers_to_run)}/{len(tickers)} ticker(s) — "
        f"{depth_label} — {trade_date} — {num_workers} worker(s)[/bold cyan]"
    ))
    console.print()

    results: list[dict] = []
    results_lock = threading.Lock()

    # Seed results with previously completed tickers
    for t in completed_tickers:
        ticker_dir = screening_dir / t
        brief_path = ticker_dir / "earnings_brief.md"
        brief_text = brief_path.read_text(encoding="utf-8") if brief_path.exists() else ""
        from tradingagents.earnings.scorer import parse_score as _parse_score
        score = _parse_score(brief_text) if brief_text else {}
        results.append({
            "ticker":       t,
            "sector":       _fetch_sector(t),
            "ta_decision":  "RESUMED",
            "brief":        brief_text,
            "earnings_date":    score.get("earnings_date", "unknown"),
            "beat_score":       score.get("beat_score", 0),
            "guidance_score":   score.get("guidance_score", 0),
            "setup_score":      score.get("setup_score", 0),
            "total_score":      score.get("total_score", 0),
            "signal":           score.get("signal", "?"),
            "confidence":       score.get("confidence", "?"),
            "one_liner":        score.get("one_liner", ""),
        })

    def process(ticker: str, worker_config: dict) -> None:
        ticker_dir = screening_dir / ticker
        ticker_dir.mkdir(exist_ok=True)
        sector = _fetch_sector(ticker)
        try:
            ta = TradingAgentsGraph(debug=False, config=worker_config)
            final_state, decision = ta.propagate(ticker, trade_date)
            save_report_to_disk(final_state, ticker, ticker_dir)
            layer = EarningsLayer(llm=ta.deep_thinking_llm, news_lookback_days=90)
            brief, score = layer.analyze_and_score(ticker, trade_date, final_state, save_dir=str(ticker_dir))
            console.print(f"  [dim][{ticker}] Fundamentals: {score.get('fundamentals_score', 0):+d}/5 ({score.get('bs_quality', '?')})[/dim]")
            pricing = None
            try:
                pricing = fetch_pricing_context(ticker, score.get("earnings_date"))
                (ticker_dir / "pricing.json").write_text(json.dumps(pricing, indent=2), encoding="utf-8")
            except Exception:
                pass
            try:
                asym = build_asymmetry(
                    ticker,
                    beat_score=score.get("beat_score"),
                    implied_move_pct=(pricing or {}).get("implied_move_pct"),
                )
                (ticker_dir / "asymmetry.json").write_text(json.dumps(asym, indent=2), encoding="utf-8")
            except Exception:
                pass
            try:
                crowding = fetch_crowding(ticker, sector=sector)
                (ticker_dir / "crowding.json").write_text(json.dumps(crowding, indent=2), encoding="utf-8")
            except Exception:
                pass
            result = {
                "ticker":       ticker,
                "sector":       sector,
                "ta_decision":  decision,
                "brief":        brief,
                "earnings_date":        score.get("earnings_date", "unknown"),
                "beat_score":           score.get("beat_score", 0),
                "guidance_score":       score.get("guidance_score", 0),
                "setup_score":          score.get("setup_score", 0),
                "total_score":          score.get("total_score", 0),
                "signal":               score.get("signal", "?"),
                "confidence":           score.get("confidence", "?"),
                "one_liner":            score.get("one_liner", ""),
                "fundamentals_score":   score.get("fundamentals_score", 0),
                "bs_quality":           score.get("bs_quality", "Adequate"),
                "margin_trend":         score.get("margin_trend", "Stable"),
                "growth_quality":       score.get("growth_quality", "Medium"),
                "fundamentals_summary": score.get("fundamentals_summary", ""),
            }
        except Exception as exc:
            result = {
                "ticker": ticker, "sector": sector, "ta_decision": "ERROR", "brief": "",
                "earnings_date": "unknown", "beat_score": 0, "guidance_score": 0,
                "setup_score": 0, "total_score": -99, "signal": "ERROR",
                "confidence": "—", "one_liner": str(exc),
            }
        with results_lock:
            results.append(result)
            n = len(results)
            sig = result.get("signal", "?")
            tot = result.get("total_score", "?")
            score_str = f"{tot:+d}" if isinstance(tot, int) else str(tot)
            console.print(
                f"  [{n}/{len(tickers)}] [cyan]{ticker}[/cyan] → "
                f"[bold]{sig}[/bold]  total: {score_str}  "
                f"[dim]saved → {ticker_dir.name}/[/dim]"
            )

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _make_worker_config(idx: int) -> dict:
        wcfg = config.copy()
        if _api_keys:
            wcfg["api_key"] = _api_keys[idx % len(_api_keys)]
        return wcfg

    if num_workers == 1:
        for _i, _ticker in enumerate(tickers_to_run):
            process(_ticker, _make_worker_config(_i))
    else:
        with ThreadPoolExecutor(max_workers=num_workers) as _pool:
            _futs = {
                _pool.submit(process, _ticker, _make_worker_config(_i)): _ticker
                for _i, _ticker in enumerate(tickers_to_run)
            }
            for _fut in as_completed(_futs):
                try:
                    _fut.result()
                except Exception:
                    pass

    sorted_results = sorted(results, key=lambda r: r.get("total_score", 0), reverse=True)
    console.print()

    def sc(n: int) -> str:
        style = "green" if n > 0 else ("red" if n < 0 else "dim")
        return f"[{style}]{n:+d}[/{style}]"

    table = Table(
        box=box.ROUNDED,
        title=f"[bold]Earnings Screener — {depth_label} — {trade_date}[/bold]",
        show_lines=True,
    )
    table.add_column("#",         justify="right", style="dim", width=3)
    table.add_column("Ticker",    style="cyan bold", width=8)
    table.add_column("Sector",    width=16)
    table.add_column("Earnings",  width=12)
    table.add_column("Beat",      justify="center", width=6)
    table.add_column("Guidance",  justify="center", width=9)
    table.add_column("Setup",     justify="center", width=7)
    table.add_column("Total",     justify="center", width=7)
    table.add_column("Signal",    justify="center", width=8)
    table.add_column("Conf.",     justify="center", width=7)
    table.add_column("One-liner", no_wrap=False, min_width=30)

    for i, r in enumerate(sorted_results, 1):
        total  = r.get("total_score", 0)
        signal = r.get("signal", "?")
        signal_color = {"BUY": "green", "SHORT": "red", "SKIP": "yellow"}.get(signal, "white")
        total_color  = "green" if total > 0 else ("red" if total < 0 else "dim")
        table.add_row(
            str(i), r["ticker"], r.get("sector", "Unknown"), r.get("earnings_date", "unknown"),
            sc(r.get("beat_score", 0)), sc(r.get("guidance_score", 0)), sc(r.get("setup_score", 0)),
            f"[{total_color}]{total:+d}[/{total_color}]",
            f"[{signal_color}]{signal}[/{signal_color}]",
            r.get("confidence", "?"), r.get("one_liner", ""),
        )
    console.print(table)

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

    if allocation_report:
        console.print(Panel(
            Markdown(allocation_report),
            title=f"[bold magenta]Portfolio Allocation — ${budget:,}[/bold magenta]",
            border_style="magenta", padding=(1, 2),
        ))
        alloc_data  = parse_allocation(allocation_report)
        allocations = alloc_data.get("allocations", [])
        if allocations:
            alloc_table = Table(box=box.ROUNDED, title="[bold]Allocation Summary[/bold]", show_lines=True)
            alloc_table.add_column("Ticker",    style="cyan bold", width=8)
            alloc_table.add_column("Direction", justify="center",  width=10)
            alloc_table.add_column("Amount",    justify="right",   width=12)
            alloc_table.add_column("% Budget",  justify="center",  width=9)
            alloc_table.add_column("Conviction",justify="center",  width=10)
            alloc_table.add_column("Rationale", no_wrap=False, min_width=30)
            for a in allocations:
                direction = a.get("direction", "SKIP")
                dir_color = {"BUY": "green", "SHORT": "red", "SKIP": "dim"}.get(direction, "white")
                amount    = a.get("amount", 0)
                alloc_table.add_row(
                    a.get("ticker", ""),
                    f"[{dir_color}]{direction}[/{dir_color}]",
                    f"[{dir_color}]{'$'+f'{amount:,}' if amount else '—'}[/{dir_color}]",
                    f"{a.get('pct_of_budget', 0):.1f}%",
                    a.get("conviction", ""),
                    a.get("rationale", ""),
                )
            deployed = alloc_data.get("total_deployed", 0)
            cash     = alloc_data.get("cash_reserved", 0)
            console.print(alloc_table)
            console.print(
                f"  Deployed: [green]${deployed:,}[/green]  "
                f"Cash: [yellow]${cash:,}[/yellow]  "
                f"Long: [green]${alloc_data.get('long_exposure', 0):,}[/green]  "
                f"Short: [red]${alloc_data.get('short_exposure', 0):,}[/red]"
            )

    console.print(f"\n[green]✓ Results saved to:[/green] {screening_dir.resolve()}")
    console.print(f"  [dim]screening_table.md[/dim]  ← ranked table")
    if allocation_report:
        console.print(f"  [dim]allocation.md[/dim]  ← portfolio allocation")
    for r in sorted_results:
        console.print(f"  [dim]{r['ticker']}/[/dim]  ← complete_report.md + earnings_brief.md")

    from cli.commands.reports import _auto_build_web
    _auto_build_web()


def screen(
    budget: int = typer.Option(100_000, "--budget", help="Capital budget for allocation manager ($)"),
):
    """Run the earnings screener across a batch of tickers."""
    run_screening(budget=budget)
