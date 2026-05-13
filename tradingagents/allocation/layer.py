"""AllocationLayer — allocates a fixed budget across screened tickers."""

import json
import re
from datetime import datetime
from pathlib import Path

from tradingagents.allocation.analyst import create_allocation_analyst
from tradingagents.allocation.council import run_council
from tradingagents.allocation.weights import apply_weights, load_weights


def _cut(text: str, max_chars: int) -> str:
    """Truncate at the last sentence boundary within max_chars."""
    if len(text) <= max_chars:
        return text
    chunk = text[:max_chars]
    last = max(chunk.rfind("."), chunk.rfind("!"), chunk.rfind("?"))
    if last > max_chars // 2:
        return chunk[: last + 1].strip()
    return chunk.strip() + "…"


class AllocationLayer:
    """Allocates a fixed capital budget across a batch of screened tickers.

    Uses an AI council by default: five advisors with different analytical
    styles review all tickers in parallel, cross-review each other, then a
    synthesis pass produces the final allocation.

    Usage:
        layer = AllocationLayer(llm=ta.deep_thinking_llm, budget=100_000)
        report = layer.allocate(
            results=sorted_results,
            trade_date="2026-05-01",
            screening_dir="reports/screening_2026-05-01_...",
            save=True,
        )
    """

    def __init__(self, llm, budget: int = 100_000, use_council: bool = True):
        self.llm = llm
        self.budget = budget
        self.use_council = use_council

    def allocate(
        self,
        results: list[dict],
        trade_date: str,
        screening_dir: str | Path = None,
        save: bool = True,
        progress_cb=None,
    ) -> str:
        """Run allocation analysis and return the report as a markdown string."""
        weights = load_weights()
        raw_contexts = self._build_contexts(results, screening_dir)
        # Inject weighted scores into every context
        contexts = [apply_weights(ctx, weights) for ctx in raw_contexts]

        if self.use_council:
            report = run_council(
                llm=self.llm,
                ticker_contexts=contexts,
                budget=self.budget,
                trade_date=trade_date,
                weights=weights,
                progress_cb=progress_cb,
            )
        else:
            analyst = create_allocation_analyst(self.llm, self.budget, trade_date)
            report = analyst(contexts)

        if save and screening_dir:
            out = Path(screening_dir) / "allocation.md"
            out.write_text(report, encoding="utf-8")

        return report

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_contexts(
        self, results: list[dict], screening_dir: str | Path = None
    ) -> list[dict]:
        """Enrich each result dict with PM decision and brief summary from disk."""
        base = Path(screening_dir) if screening_dir else None
        contexts = []

        for r in results:
            ticker = r["ticker"]
            ctx = dict(r)
            if "sector" not in ctx:
                ctx["sector"] = "Unknown"

            if base:
                # Portfolio manager decision — full text up to 1500 chars
                pm_path = base / ticker / "5_portfolio" / "decision.md"
                if pm_path.exists():
                    ctx["pm_decision"] = _cut(pm_path.read_text(encoding="utf-8").strip(), 1500)
                else:
                    ctx["pm_decision"] = ctx.get("ta_decision", "Not available")

                # Earnings brief — strip score JSON block, keep up to 1200 chars
                brief_path = base / ticker / "earnings_brief.md"
                if brief_path.exists():
                    raw = brief_path.read_text(encoding="utf-8")
                    clean = re.sub(
                        r"### Scores\s*```json.*?```", "", raw, flags=re.DOTALL
                    ).strip()
                    ctx["brief_summary"] = _cut(clean, 1200)
                else:
                    ctx["brief_summary"] = ctx.get("one_liner", "Not available")

            contexts.append(ctx)

        return contexts


def parse_allocation(report: str) -> dict:
    """Extract the JSON allocation block from the report."""
    match = re.search(r"```json\s*(\{.*?\})\s*```", report, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError):
        return {}
