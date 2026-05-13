"""ReflectionLayer — post-mortem analysis on completed trades."""

import json
import re
from datetime import datetime
from pathlib import Path

from tradingagents.reflection.data_fetcher import fetch_reflection_context
from tradingagents.reflection.analyst import create_reflection_analyst


class ReflectionLayer:
    """Post-mortem analysis for a completed trade.

    Usage:
        layer = ReflectionLayer(llm=ta.deep_thinking_llm)
        post_mortem = layer.analyze(
            ticker="CLS",
            trade_date="2026-04-20",
            exit_date="2026-04-27",
            direction="BUY",
            shares=100,
            entry_price=50.0,
            exit_price=55.0,
            prior_analysis_path="reports/CLS_20260420_143022",
            save_dir="reports/reflections/CLS_20260420_20260427",
        )
    """

    def __init__(self, llm):
        self.llm = llm

    def analyze(
        self,
        ticker: str,
        trade_date: str,
        exit_date: str,
        direction: str,
        shares: float,
        entry_price: float,
        exit_price: float,
        prior_analysis_path: str = None,
        save_dir: str = None,
    ) -> str:
        """Run post-mortem analysis and return the report as a markdown string."""
        reflection_context = fetch_reflection_context(ticker, trade_date, exit_date)

        prior_analysis = {}
        if prior_analysis_path:
            analysis_path = Path(prior_analysis_path)

            earnings_brief_path = analysis_path / "earnings_brief.md"
            if earnings_brief_path.exists():
                prior_analysis["earnings_brief"] = earnings_brief_path.read_text(encoding="utf-8")

            complete_report_path = analysis_path / "complete_report.md"
            if complete_report_path.exists():
                full_report = complete_report_path.read_text(encoding="utf-8")
                pm_match = re.search(r"## V\. Portfolio Manager Decision.*$", full_report, re.DOTALL)
                if pm_match:
                    prior_analysis["final_trade_decision"] = pm_match.group(0)[:3000]
                else:
                    prior_analysis["final_trade_decision"] = full_report[-3000:]

        analyst = create_reflection_analyst(
            self.llm, ticker, trade_date, exit_date, direction, entry_price, exit_price, shares
        )
        post_mortem = analyst(reflection_context, prior_analysis)

        if save_dir:
            self._save(post_mortem, save_dir)

        return post_mortem

    def _save(self, post_mortem: str, save_dir: str) -> None:
        out = Path(save_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "post_mortem.md").write_text(post_mortem, encoding="utf-8")


def parse_reflection_score(post_mortem: str) -> dict:
    """Extract the JSON score block from a post-mortem report."""
    match = re.search(r"```json\s*(\{.*?\})\s*```", post_mortem, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError):
        return {}
