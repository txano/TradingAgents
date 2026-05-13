from pathlib import Path

from dotenv import load_dotenv

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.earnings import EarningsLayer
from tradingagents.graph.trading_graph import TradingAgentsGraph

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "google"
config["deep_think_llm"] = "gemini-3.1-pro-preview"
config["quick_think_llm"] = "gemini-2.5-flash"
config["max_debate_rounds"] = 1

# ---------------------------------------------------------------------------
# Step 1 — run the standard TradingAgents pipeline
# ---------------------------------------------------------------------------
ta = TradingAgentsGraph(debug=True, config=config)

TICKER = "CLS"
TRADE_DATE = "2026-04-24"

final_state, decision = ta.propagate(TICKER, TRADE_DATE)
print(f"\nStandard decision: {decision}\n")

# ---------------------------------------------------------------------------
# Step 2 — run the Earnings Layer on top of the existing output
# ---------------------------------------------------------------------------
# Point save_dir at the same folder where the existing reports landed so the
# earnings_brief.md sits alongside the other analyst reports.
reports_dir = Path(config.get("results_dir", "reports")) / TICKER

earnings_layer = EarningsLayer(
    llm=ta.deep_thinking_llm,
    news_lookback_days=90,  # pull 3 months of news and analyst changes
)

brief = earnings_layer.analyze(
    ticker=TICKER,
    trade_date=TRADE_DATE,
    final_state=final_state,
    save_dir=str(reports_dir),
)

print(brief)
