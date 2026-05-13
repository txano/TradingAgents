"""EarningsLayer — runs on top of TradingAgentsGraph output to produce a pre-earnings brief."""

import json
from datetime import datetime
from pathlib import Path

from tradingagents.earnings.data_fetcher import fetch_earnings_context
from tradingagents.earnings.analyst import create_earnings_analyst
from tradingagents.earnings.scorer import parse_score


class EarningsLayer:
    """Post-processes TradingAgentsGraph output with an earnings-focused analysis.

    Usage:
        final_state, decision = ta.propagate("NVDA", "2024-05-10")
        layer = EarningsLayer(llm=ta.deep_thinking_llm)
        brief = layer.analyze("NVDA", "2024-05-10", final_state)
        print(brief)
    """

    def __init__(self, llm, news_lookback_days: int = 90):
        """
        Args:
            llm: LangChain-compatible chat model (use ta.deep_thinking_llm).
            news_lookback_days: How many days of news and analyst changes to pull.
        """
        self.llm = llm
        self.news_lookback_days = news_lookback_days

    def analyze(
        self,
        ticker: str,
        trade_date: str,
        final_state: dict,
        save_dir: str = None,
    ) -> str:
        """Run the earnings analysis and return the brief as a markdown string.

        Args:
            ticker: Stock ticker symbol.
            trade_date: Analysis date (yyyy-mm-dd).
            final_state: The dict returned by TradingAgentsGraph.propagate().
            save_dir: Optional directory to save earnings_brief.md alongside the
                      existing reports. If None the brief is only returned, not saved.

        Returns:
            Earnings brief as a markdown string.
        """
        earnings_context = fetch_earnings_context(
            ticker, trade_date, news_lookback_days=self.news_lookback_days
        )

        existing_reports = {
            "market_report": final_state.get("market_report", ""),
            "news_report": final_state.get("news_report", ""),
            "sentiment_report": final_state.get("sentiment_report", ""),
            "fundamentals_report": final_state.get("fundamentals_report", ""),
            "final_trade_decision": final_state.get("final_trade_decision", ""),
        }

        analyst = create_earnings_analyst(self.llm, ticker, trade_date)
        brief = analyst(earnings_context, existing_reports)

        if save_dir:
            self._save(brief, earnings_context, save_dir, ticker, trade_date)

        return brief

    def analyze_and_score(
        self,
        ticker: str,
        trade_date: str,
        final_state: dict,
        save_dir: str = None,
    ) -> tuple[str, dict]:
        """Run earnings analysis and return (brief, score_dict).

        score_dict keys: earnings_date, beat_score, guidance_score, setup_score,
        total_score, signal, confidence, one_liner.
        """
        brief = self.analyze(ticker, trade_date, final_state, save_dir=save_dir)
        score = parse_score(brief)
        return brief, score

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _save(
        self,
        brief: str,
        earnings_context: dict,
        save_dir: str,
        ticker: str,
        trade_date: str,
    ):
        out = Path(save_dir)
        out.mkdir(parents=True, exist_ok=True)

        (out / "earnings_brief.md").write_text(brief, encoding="utf-8")

        # Also save the raw data used so you can audit/replay
        raw_data = {k: v for k, v in earnings_context.items() if k != "recent_news"}
        raw_data["recent_news_snippet"] = (
            earnings_context.get("recent_news", "")[:500] + "..."
        )
        raw_data["ticker"] = ticker
        raw_data["trade_date"] = trade_date
        raw_data["generated_at"] = datetime.now().isoformat()

        (out / "earnings_raw_data.json").write_text(
            json.dumps(raw_data, indent=2, default=str), encoding="utf-8"
        )
