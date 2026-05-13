"""
Tier 1 earnings screener.

Edit TICKERS and TRADE_DATE below, then run:
    uv run python screen.py

Each ticker runs the fast TradingAgents analysis (1 debate round) followed by
the Earnings Layer. Results are ranked by total score and saved to
reports/screening_YYYY-MM-DD.md
"""

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from rich import box
from rich.console import Console
from rich.rule import Rule
from rich.table import Table

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.earnings import EarningsLayer
from tradingagents.graph.trading_graph import TradingAgentsGraph

load_dotenv()

# ─── Edit these ────────────────────────────────────────────────────────────────
TICKERS = [
    "CLS",
    # Add tickers reporting next week, e.g.:
    # "AAPL", "MSFT", "NVDA", "META", "GOOGL",
]

TRADE_DATE = date.today().isoformat()

# Increase WORKERS to run tickers in parallel (2–3 is practical; watch API rate limits).
# Set to 1 for sequential execution.
WORKERS = 1
# ───────────────────────────────────────────────────────────────────────────────

FAST_CONFIG = DEFAULT_CONFIG.copy()
FAST_CONFIG["llm_provider"] = "deepseek"
FAST_CONFIG["deep_think_llm"] = "deepseek-v4-pro"
FAST_CONFIG["quick_think_llm"] = "deepseek-v4-flash"
FAST_CONFIG["max_debate_rounds"] = 1
FAST_CONFIG["max_risk_discuss_rounds"] = 1

console = Console()
_results_lock = threading.Lock()
_results: list[dict] = []


def _run_ticker(ticker: str) -> dict:
    ta = TradingAgentsGraph(debug=False, config=FAST_CONFIG)
    final_state, decision = ta.propagate(ticker, TRADE_DATE)

    layer = EarningsLayer(llm=ta.deep_thinking_llm, news_lookback_days=90)
    brief, score = layer.analyze_and_score(ticker, TRADE_DATE, final_state)

    return {
        "ticker": ticker,
        "ta_decision": decision,
        "brief": brief,
        "earnings_date": score.get("earnings_date", "unknown"),
        "beat_score": score.get("beat_score", 0),
        "guidance_score": score.get("guidance_score", 0),
        "setup_score": score.get("setup_score", 0),
        "total_score": score.get("total_score", 0),
        "signal": score.get("signal", "?"),
        "confidence": score.get("confidence", "?"),
        "one_liner": score.get("one_liner", ""),
    }


def _score_cell(n: int) -> str:
    style = "green" if n > 0 else ("red" if n < 0 else "dim")
    return f"[{style}]{n:+d}[/{style}]"


def _build_table(rows: list[dict]) -> Table:
    table = Table(
        box=box.ROUNDED,
        title=f"[bold]Earnings Screener — {TRADE_DATE}[/bold]",
        show_lines=True,
    )
    table.add_column("#", justify="right", style="dim", width=3)
    table.add_column("Ticker", style="cyan bold", width=8)
    table.add_column("Earnings", width=12)
    table.add_column("Beat", justify="center", width=6)
    table.add_column("Guidance", justify="center", width=9)
    table.add_column("Setup", justify="center", width=7)
    table.add_column("Total", justify="center", width=7)
    table.add_column("Signal", justify="center", width=8)
    table.add_column("Conf.", justify="center", width=7)
    table.add_column("One-liner", no_wrap=False, min_width=30)

    for i, r in enumerate(rows, 1):
        total = r.get("total_score", 0)
        signal = r.get("signal", "?")
        signal_color = {"BUY": "green", "SHORT": "red", "SKIP": "yellow"}.get(signal, "white")
        total_color = "green" if total > 0 else ("red" if total < 0 else "dim")

        table.add_row(
            str(i),
            r["ticker"],
            r.get("earnings_date", "unknown"),
            _score_cell(r.get("beat_score", 0)),
            _score_cell(r.get("guidance_score", 0)),
            _score_cell(r.get("setup_score", 0)),
            f"[{total_color}]{total:+d}[/{total_color}]",
            f"[{signal_color}]{signal}[/{signal_color}]",
            r.get("confidence", "?"),
            r.get("one_liner", ""),
        )

    return table


def _save_results(rows: list[dict], out_path: Path) -> None:
    lines = [
        f"# Earnings Screener — {TRADE_DATE}\n\n",
        "| # | Ticker | Earnings | Beat | Guidance | Setup | Total | Signal | Confidence | One-liner |\n",
        "|---|--------|----------|------|----------|-------|-------|--------|------------|-----------|\n",
    ]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"| {i} | {r['ticker']} | {r.get('earnings_date','?')} "
            f"| {r.get('beat_score',0):+d} | {r.get('guidance_score',0):+d} "
            f"| {r.get('setup_score',0):+d} | {r.get('total_score',0):+d} "
            f"| {r.get('signal','?')} | {r.get('confidence','?')} "
            f"| {r.get('one_liner','')} |\n"
        )

    lines.append("\n---\n\n## Individual Briefs\n\n")
    for r in rows:
        if r.get("brief"):
            lines.append(f"\n{r['brief']}\n\n---\n")

    out_path.write_text("".join(lines), encoding="utf-8")


def main() -> None:
    console.print()
    console.print(Rule(f"[bold cyan]Earnings Screener[/bold cyan] — {len(TICKERS)} tickers — {TRADE_DATE}"))
    console.print()

    completed = 0

    def process(ticker: str) -> dict:
        nonlocal completed
        try:
            result = _run_ticker(ticker)
        except Exception as exc:
            result = {
                "ticker": ticker,
                "ta_decision": "ERROR",
                "brief": "",
                "earnings_date": "unknown",
                "beat_score": 0,
                "guidance_score": 0,
                "setup_score": 0,
                "total_score": -99,
                "signal": "ERROR",
                "confidence": "—",
                "one_liner": str(exc),
            }
        with _results_lock:
            _results.append(result)
            completed += 1
            signal = result.get("signal", "?")
            total = result.get("total_score", "?")
            score_str = f"{total:+d}" if isinstance(total, int) else str(total)
            console.print(
                f"  [{completed}/{len(TICKERS)}] [cyan]{ticker}[/cyan] → "
                f"[bold]{signal}[/bold]  total: {score_str}"
            )
        return result

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(process, t): t for t in TICKERS}
        for _ in as_completed(futures):
            pass

    sorted_results = sorted(_results, key=lambda r: r.get("total_score", 0), reverse=True)

    console.print()
    console.print(_build_table(sorted_results))

    out_path = Path("reports") / f"screening_{TRADE_DATE}.md"
    out_path.parent.mkdir(exist_ok=True)
    _save_results(sorted_results, out_path)
    console.print(f"\n[green]✓ Saved to {out_path}[/green]\n")


if __name__ == "__main__":
    main()
