"""Shared helpers used across CLI command modules."""

import datetime
import json
import logging
from pathlib import Path

from rich.console import Console

console = Console()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Trades path — stored inside reports/ so Syncthing keeps Mac ↔ Pi in sync.
# Auto-migrates from the old ~/.tradingagents/trades.json on first access.
# ---------------------------------------------------------------------------

_LEGACY_TRADES = Path.home() / ".tradingagents" / "trades.json"


def _trades_path() -> Path:
    """Return the canonical trades.json path: reports/trades.json.

    On first call, migrates data from ~/.tradingagents/trades.json into the
    new location so existing trade history is preserved.
    """
    new_path = Path("reports") / "trades.json"
    if not new_path.exists() and _LEGACY_TRADES.exists():
        try:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            new_path.write_bytes(_LEGACY_TRADES.read_bytes())
            logger.info("Migrated trades.json from %s → %s", _LEGACY_TRADES, new_path)
        except Exception as exc:
            logger.warning("Could not migrate trades.json: %s", exc)
    return new_path


def _fetch_sector(ticker: str) -> str:
    """Fetch sector from yfinance, returns 'Unknown' on failure."""
    try:
        import yfinance as yf
        return yf.Ticker(ticker).info.get("sector") or "Unknown"
    except Exception:
        return "Unknown"


# Provider → env var holding its API key. Numbered variants (KEY, KEY_2 … KEY_8)
# let a screen/dashboard run round-robin multiple keys across workers/advisors.
_PROVIDER_KEY_ENV = {
    "deepseek":   "DEEPSEEK_API_KEY",
    "xai":        "XAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "qwen":       "DASHSCOPE_API_KEY",
    "glm":        "ZHIPU_API_KEY",
    "openai":     "OPENAI_API_KEY",
    "anthropic":  "ANTHROPIC_API_KEY",
}


def gather_api_keys(provider: str) -> list[str]:
    """Collect a provider's API keys from the environment, in numeric order.

    Reads `KEY` plus every `KEY_<n>` that is set, skipping blanks and duplicates.
    Returns [] when the provider is unknown or unset.

    The suffix list used to stop at `_8`, so a .env holding 16 keys silently
    contributed 8 — and a 16-worker screen then ran two concurrent streams per
    key, which looks exactly like a rate limit. Scan the environment instead of
    hard-coding a ceiling, so adding a key is all it takes to use it. Gaps are
    tolerated: `KEY_9` counts even if `KEY_7` is missing.
    """
    import os
    import re

    base_env = _PROVIDER_KEY_ENV.get((provider or "").lower(), "")
    if not base_env:
        return []
    pattern = re.compile(rf"^{re.escape(base_env)}(?:_(\d+))?$")
    found: list[tuple[int, str]] = []
    for name, value in os.environ.items():
        m = pattern.match(name)
        if not m:
            continue
        v = (value or "").strip()
        if v:
            found.append((int(m.group(1) or 1), v))   # bare KEY sorts first
    keys: list[str] = []
    for _, v in sorted(found):
        if v not in keys:
            keys.append(v)
    return keys


def save_report_to_disk(final_state: dict, ticker: str, save_path: Path) -> Path:
    """Save complete analysis report to disk with organised subfolders."""
    save_path.mkdir(parents=True, exist_ok=True)
    sections = []

    analysts_dir = save_path / "1_analysts"
    analyst_parts = []
    for key, fname, label in [
        ("market_report",       "market.md",       "Market Analyst"),
        ("sentiment_report",    "sentiment.md",    "Sentiment Analyst"),
        ("news_report",         "news.md",         "News Analyst"),
        ("fundamentals_report", "fundamentals.md", "Fundamentals Analyst"),
    ]:
        if final_state.get(key):
            analysts_dir.mkdir(exist_ok=True)
            (analysts_dir / fname).write_text(final_state[key], encoding="utf-8")
            analyst_parts.append((label, final_state[key]))
    if analyst_parts:
        content = "\n\n".join(f"### {n}\n{t}" for n, t in analyst_parts)
        sections.append(f"## I. Analyst Team Reports\n\n{content}")

    if final_state.get("investment_debate_state"):
        research_dir = save_path / "2_research"
        debate = final_state["investment_debate_state"]
        research_parts = []
        for key, fname, label in [
            ("bull_history",   "bull.md",     "Bull Researcher"),
            ("bear_history",   "bear.md",     "Bear Researcher"),
            ("judge_decision", "manager.md",  "Research Manager"),
        ]:
            if debate.get(key):
                research_dir.mkdir(exist_ok=True)
                (research_dir / fname).write_text(debate[key], encoding="utf-8")
                research_parts.append((label, debate[key]))
        if research_parts:
            content = "\n\n".join(f"### {n}\n{t}" for n, t in research_parts)
            sections.append(f"## II. Research Team Decision\n\n{content}")

    if final_state.get("trader_investment_plan"):
        trading_dir = save_path / "3_trading"
        trading_dir.mkdir(exist_ok=True)
        (trading_dir / "trader.md").write_text(final_state["trader_investment_plan"], encoding="utf-8")
        sections.append(f"## III. Trading Team Plan\n\n### Trader\n{final_state['trader_investment_plan']}")

    if final_state.get("risk_debate_state"):
        risk_dir = save_path / "4_risk"
        risk = final_state["risk_debate_state"]
        risk_parts = []
        for key, fname, label in [
            ("aggressive_history",  "aggressive.md",  "Aggressive Analyst"),
            ("conservative_history","conservative.md","Conservative Analyst"),
            ("neutral_history",     "neutral.md",     "Neutral Analyst"),
        ]:
            if risk.get(key):
                risk_dir.mkdir(exist_ok=True)
                (risk_dir / fname).write_text(risk[key], encoding="utf-8")
                risk_parts.append((label, risk[key]))
        if risk_parts:
            content = "\n\n".join(f"### {n}\n{t}" for n, t in risk_parts)
            sections.append(f"## IV. Risk Management Team Decision\n\n{content}")

        if risk.get("judge_decision"):
            portfolio_dir = save_path / "5_portfolio"
            portfolio_dir.mkdir(exist_ok=True)
            (portfolio_dir / "decision.md").write_text(risk["judge_decision"], encoding="utf-8")
            sections.append(
                f"## V. Portfolio Manager Decision\n\n### Portfolio Manager\n{risk['judge_decision']}"
            )

    header = (
        f"# Trading Analysis Report: {ticker}\n\n"
        f"Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    )
    (save_path / "complete_report.md").write_text(header + "\n\n".join(sections), encoding="utf-8")
    return save_path / "complete_report.md"


def parse_brief_scores(ticker_dir: Path) -> "dict | None":
    """Read one ticker's earnings_brief.md back into a screening result dict.

    The score JSON embedded in the brief is the durable record of a screen, so
    this is how `allocate` and `resume` rebuild a screening table from a folder
    without re-running any LLM work. Returns None when the brief is missing or
    carries no parseable JSON block.
    """
    import re as _re

    brief_path = Path(ticker_dir) / "earnings_brief.md"
    if not brief_path.exists():
        return None
    m = _re.search(r"```json\s*(\{.*?\})\s*```", brief_path.read_text(encoding="utf-8"), _re.DOTALL)
    if not m:
        return None
    try:
        scores = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    return {
        "ticker":         Path(ticker_dir).name,
        "earnings_date":  scores.get("earnings_date", "unknown"),
        "beat_score":     int(scores.get("beat_score", 0)),
        "guidance_score": int(scores.get("guidance_score", 0)),
        "setup_score":    int(scores.get("setup_score", 0)),
        "total_score":    int(scores.get("total_score", 0)),
        "signal":         scores.get("signal", "SKIP"),
        "confidence":     scores.get("confidence", "?"),
        "one_liner":      scores.get("one_liner", ""),
    }
