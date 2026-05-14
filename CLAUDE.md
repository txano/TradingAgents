# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install (editable, with uv)
pip install .          # or: uv pip install -e .

# Run the interactive CLI
tradingagents          # installed entry point
python -m cli.main     # from source

# Single-ticker deep-dive
tradingagents analyze

# Batch earnings screen + AI Council allocation
tradingagents screen

# Post-trade reflection / calibration
tradingagents reflect
tradingagents calibrate

# Web dashboard (http://127.0.0.1:8765)
tradingagents dashboard

# Run tests
python -m pytest tests/
python -m pytest tests/test_model_validation.py   # single file

# Run the legacy screen.py script (edit TICKERS/TRADE_DATE inside first)
uv run python screen.py
```

Environment: copy `.env.example` to `.env` and fill in API keys. Enterprise providers (Azure, Bedrock) use `.env.enterprise`.

## Architecture

### Top-level flow

```
CLI (cli/main.py, Typer + Rich)
  ↓
TradingAgentsGraph   — LangGraph multi-agent pipeline (tradingagents/graph/)
EarningsLayer        — pre-earnings brief + scoring (tradingagents/earnings/)
AllocationLayer      — AI Council capital allocation (tradingagents/allocation/)
ReflectionLayer      — post-trade reflection (tradingagents/reflection/)
CalibrationLayer     — prediction accuracy (tradingagents/calibration/)
IBKRLayer            — trade import via Flex XML (tradingagents/ibkr/)
```

### LangGraph pipeline (`tradingagents/graph/`)

`TradingAgentsGraph.propagate(ticker, date)` runs five agent teams in sequence:

| Team | Agents | Module |
|------|--------|--------|
| 1 — Analysts | market, fundamentals, news, social | `tradingagents/agents/analysts/` |
| 2 — Researchers | bull_researcher, bear_researcher (debate) | `tradingagents/agents/researchers/` |
| 3 — Research Mgr | synthesizes researcher debate | `tradingagents/agents/managers/` |
| 4 — Risk Mgmt | aggressive / conservative / neutral debaters | `tradingagents/agents/risk_mgmt/` |
| 5 — Portfolio Mgr | final BUY / SHORT / SKIP decision | `tradingagents/agents/` |

Graph wiring lives in `graph/setup.py`; routing logic in `graph/conditional_logic.py`; the `propagate()` call in `graph/propagation.py`.

### LLM abstraction (`tradingagents/llm_clients/`)

`create_llm_client(provider, model, ...)` (factory.py) returns a provider-specific client inheriting `BaseLLMClient`. Supported providers: `openai`, `anthropic`, `google`, `xai`, `deepseek`, `qwen`, `glm`, `azure`, `openrouter`, `ollama`. The `model_catalog.py` lists known models per provider; `validators.py` warns (not errors) on unknown models.

`DEFAULT_CONFIG` (`tradingagents/default_config.py`) is the single source of truth for LLM settings, debate rounds, and data vendor selection. Always copy it before mutating: `config = DEFAULT_CONFIG.copy()`.

### Data layer (`tradingagents/dataflows/`)

`interface.py` exposes abstract tool functions (`get_stock_data`, `get_news`, `get_fundamentals`, etc.) that dispatch to the configured vendor (`yfinance` or `alpha_vantage`) based on `config["data_vendors"]`. Tool-level overrides via `config["tool_vendors"]` take precedence. Agents call these via `tradingagents/agents/utils/agent_utils.py`.

### Earnings → Scoring → Allocation loop

1. `EarningsLayer.analyze_and_score(ticker, date, state)` → `earnings_brief.md` + JSON scores (`beat_score`, `guidance_score`, `setup_score`, each −5 to +5).
2. `weights.py` computes `weighted_score = Σ weight_i × score_i`; weights stored at `~/.tradingagents/allocation_weights.json`.
3. `council.py` runs 11 LLM calls in 3 rounds (5 advisors → 5 cross-reviews → 1 synthesis) to allocate a capital budget.

### Persistent state

| What | Location |
|------|----------|
| Trade log | `~/.tradingagents/trades.json` |
| Allocation weights | `~/.tradingagents/allocation_weights.json` |
| LLM / data cache | `~/.tradingagents/cache/` |
| Agent logs | `~/.tradingagents/logs/` |
| Reports | `./reports/` (project root) |

Report folders: `reports/screening_YYYY-MM-DD_TIMESTAMP/TICKER/` for batch runs; `reports/TICKER_YYYYMMDD_HHMMSS/` for individual `analyze` runs.

### CLI web server (`cli/server.py`)

FastAPI + WebSockets server backing the `dashboard` command. The static frontend is at `cli/static/dashboard.html`. The `Run` tab submits analysis jobs that execute remotely via the server.
