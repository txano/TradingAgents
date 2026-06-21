# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Keep documentation current

Treat the docs as part of the change, not an afterthought. Whenever you add or
materially change a feature, command, data artifact, or workflow, update the
relevant docs **in the same change**:
- **`ROADMAP.md`** — tick off completed items, record findings/decisions, and re-prioritise. Bump its "Last updated" date.
- **`ARCHITECTURE.md`** — the canonical reference for modules, data schemas, per-ticker artifacts, the council pipeline, and CLI commands.
- **`CLAUDE.md`** (this file) — commands, architecture overview, and persistent-state locations.

Periodically (e.g. at the end of a working session, or before committing) do a
quick consistency pass so these three never drift from the code.

## Commands

```bash
# Install (editable, with uv)
pip install .          # or: uv pip install -e .

# Run the interactive CLI (interactive menu when no subcommand given)
tradingagents
python -m cli.main

# Single-ticker deep-dive
tradingagents analyze

# Batch earnings screen + AI Council allocation
tradingagents screen [--budget 100000]

# Re-run allocation council on an existing screen folder
tradingagents allocate [--budget 100000] [--dir reports/screening_...]

# Earnings calendar → auto-screen upcoming earnings
tradingagents earnings-calendar

# Trade management
tradingagents import-ibkr  # IBKR Flex XML trade import
tradingagents trades       # view trade history table
tradingagents reflect      # post-mortem on a completed trade

# Calibration & improvement
tradingagents calibrate    # predictions vs actual outcomes
tradingagents reflect      # interactive post-mortem on chosen trade(s)
tradingagents learn        # reflect on ALL trades, then auto-apply improvements
tradingagents improve      # LLM analysis of past reflections (report only)
tradingagents correlation  # score-to-outcome correlation analysis
tradingagents stats        # win rate & accuracy summary
tradingagents allocation-weights   # view / adjust the four scoring weights

# learn flags: --provider/--model (else prompts once) · --force (redo all reflections)
#              --dry-run (apply nothing) · --skip-reflect · --no-weights · --no-prompt-edits
#              --apply-proposal PATH  (apply a reviewed proposal.json verbatim, no LLM call)
# Typical review→apply loop:
#   tradingagents learn --dry-run                                   # review the report/proposal
#   tradingagents learn --apply-proposal reports/self_improve_*/proposal.json   # apply exactly that

# Web dashboard (http://127.0.0.1:8765)
tradingagents dashboard
tradingagents build-web    # build static reports website

# Run tests
python -m pytest tests/
python -m pytest tests/test_model_validation.py   # single file
python -m pytest -m unit      # fast isolated tests only
python -m pytest -m smoke     # quick sanity checks

# Run the legacy screen.py script (edit TICKERS/TRADE_DATE inside first)
uv run python screen.py
```

Environment: copy `.env.example` to `.env` and fill in API keys. Enterprise providers (Azure, Bedrock) use `.env.enterprise`.

## Architecture

### Top-level flow

```
CLI (cli/main.py + cli/commands/, Typer + Rich)
  ↓
TradingAgentsGraph   — LangGraph multi-agent pipeline (tradingagents/graph/)
EarningsLayer        — pre-earnings brief + scoring (tradingagents/earnings/)
AllocationLayer      — AI Council capital allocation (tradingagents/allocation/)
ReflectionLayer      — post-trade reflection (tradingagents/reflection/)
LearningLayer        — lessons library from reflections (tradingagents/learning/)
CalibrationLayer     — prediction accuracy (tradingagents/calibration/)
IBKRLayer            — trade import via Flex XML (tradingagents/ibkr/)
```

Subcommand implementations live in `cli/commands/` (screen, allocate, trades,
reflect, calendar, dashboard, ...); `cli/main.py` holds the interactive menu and
the `analyze` flow.

### LangGraph pipeline (`tradingagents/graph/`)

`TradingAgentsGraph.propagate(ticker, date)` runs five agent teams in sequence:

| Team | Agents | Module |
|------|--------|--------|
| 1 — Analysts | market, fundamentals, news, social | `tradingagents/agents/analysts/` |
| 2 — Researchers | bull_researcher, bear_researcher (debate) | `tradingagents/agents/researchers/` |
| 3 — Research Mgr | synthesizes researcher debate | `tradingagents/agents/managers/` |
| 4 — Risk Mgmt | aggressive / conservative / neutral debaters | `tradingagents/agents/risk_mgmt/` |
| 5 — Portfolio Mgr | final BUY / SHORT / SKIP decision | `tradingagents/agents/` |

Graph wiring lives in `graph/setup.py`; routing logic in `graph/conditional_logic.py`; the `propagate()` call in `graph/propagation.py`. `TradingAgentsGraph` exposes `.deep_thinking_llm` and `.quick_think_llm` attributes — the downstream layers (EarningsLayer, AllocationLayer, etc.) take these directly.

Checkpoint/resume: set `config["checkpoint_enabled"] = True` (or env var `TRADINGAGENTS_CHECKPOINT_ENABLED=true`) to persist LangGraph state to SQLite after each node, so a crashed run can resume from the last successful step.

### LLM abstraction (`tradingagents/llm_clients/`)

`create_llm_client(provider, model, ...)` (`factory.py`) returns a provider-specific client inheriting `BaseLLMClient`. Supported providers: `openai`, `anthropic`, `google`, `xai`, `deepseek`, `qwen`, `glm`, `azure`, `openrouter`, `ollama`. `model_catalog.py` lists known models per provider; `validators.py` warns (not errors) on unknown models.

`DEFAULT_CONFIG` (`tradingagents/default_config.py`) is the single source of truth for LLM settings, debate rounds, and data vendor selection. Always copy it before mutating: `config = DEFAULT_CONFIG.copy()`. All `TRADINGAGENTS_*` env vars listed in `_ENV_OVERRIDES` take precedence over the defaults at import time.

### Structured-output agent pattern

Three agents (Portfolio Manager, Trader, Research Manager) use a shared two-step pattern defined in `tradingagents/agents/utils/structured.py`:

1. `bind_structured(llm, Schema, agent_name)` — wraps the LLM with `with_structured_output(Schema)`; returns `None` if the provider doesn't support it.
2. `invoke_structured_or_freetext(structured_llm, plain_llm, prompt, render, agent_name)` — runs the typed call and renders to markdown; falls back to plain `llm.invoke` on any failure.

Pydantic schemas live in `tradingagents/agents/schemas.py`.

### Data layer (`tradingagents/dataflows/`)

`interface.py` exposes abstract tool functions (`get_stock_data`, `get_news`, `get_fundamentals`, etc.) that dispatch to the configured vendor (`yfinance` or `alpha_vantage`) based on `config["data_vendors"]`. Tool-level overrides via `config["tool_vendors"]` take precedence. Agents call these via `tradingagents/agents/utils/agent_utils.py`.

### Earnings → Scoring → Allocation loop

1. `EarningsLayer.analyze_and_score(ticker, date, state)` → `earnings_brief.md` + JSON scores (`beat_score`, `guidance_score`, `setup_score`, each −5 to +5) plus a `fundamentals_score` (−5 to +5) from `allocation/fundamentals_scorer.py`, which grounds the LLM in hard yfinance metrics (margins, FCF, debt/EBITDA) saved to `fundamentals_score.json`. Pricing context (spot, fwd P/E, options-implied earnings move) is saved to `pricing.json` by `allocation/pricing.py`. Payoff asymmetry (E[move|beat], E[move|miss], fade rate, coverage ratio, and EV of a long) is computed from the last ~8 historical prints by `allocation/asymmetry.py` and saved to `asymmetry.json`; both feed the council. `validator.py` enforces the EV gate (#14b): a long with computable `EV ≤ 0` or `EV/implied_move < 0.25` is a hard violation (re-prompt); quality names with null EV but high fade / low coverage get a soft "size down" advisory (`asymmetry_advisories`), not a skip. Crowding/run-up signals (1m/3m return vs sector ETF, distance from 52w high, EPS-revision momentum) are computed by `allocation/crowding.py`, saved to `crowding.json`, shown on the council `Crowding:` line, and surfaced as soft `crowding_advisories` (#14c) — both soft advisory types render in one combined **Sizing Advisories** report section.
2. `weights.py` computes `weighted_score = Σ weight_i × score_i` over the four buckets (defaults: beat 0.7, guidance 1.0, setup 1.0, fundamentals 1.5 — single source `weights.DEFAULTS`); stored at `~/.tradingagents/allocation_weights.json`. Signal thresholds in the council prompt scale with these weights.
3. `council.py` runs 11 LLM calls in 3 rounds (5 persona advisors → 5 persona-aware cross-reviews → 1 synthesis). Failed advisors are dropped, not stubbed. Advisors can run on different models via `config["council_advisor_models"]` (list of `"provider:model"`, env `TRADINGAGENTS_COUNCIL_ADVISOR_MODELS`); synthesis always uses the main LLM.
4. `validator.py` deterministically checks the allocation (30% position cap, 35%-of-budget sector cap, max 6 positions, short rules, budget arithmetic); violations trigger one corrective re-prompt, and any that remain are appended to the report as a `### ⚠ Constraint Check` section.
5. `parse_allocation(report)` (in `allocation/common.py`, re-exported from `layer.py`) and `parse_reflection_score(post_mortem)` extract the embedded JSON blocks.
6. `learning/lessons.py` distils past reflections into a rules library (rules require ≥2 supporting trades) injected into the synthesis prompt; cached at `~/.tradingagents/lessons_cache.md`, invalidated by a digest of the reflection files.

### Automated self-improvement (`learn` command)

`tradingagents learn` runs the whole feedback loop non-interactively:
1. `learning/trade_reflections.py` — `reflect_all()` consolidates fills (by ticker+exit_date) and runs `ReflectionLayer` over every trade in `trades.json` (pending only; `--force` redoes all). The interactive `reflect` command shares `consolidate_trades()` from this module.
2. `learning/self_improve.py` — one deep-think call analyses all reflections and returns a markdown report plus a structured JSON proposal (`weights`, `prompt_edits`, `process_notes`).
3. Auto-apply (`self_improve.apply_proposal()`): scoring weights via `save_weights()`, and prompt-source edits to an **allowlist** of agent files (`EDITABLE_PROMPT_FILES`) under a safety harness — exact unique anchor match, f-string `{placeholder}` preservation, per-file backup, and a `compile()` check that restores the backup on any syntax break. Output (report, `proposal.json`, `CHANGELOG.md`, `backups/`) is written to `reports/self_improve_TIMESTAMP/`. `--dry-run` previews without applying; `--no-weights` / `--no-prompt-edits` scope what gets applied. Every change is git-visible — revert with `git diff`/`git checkout`.

Review→apply: re-running `learn` does a *fresh* analysis (new LLM call → possibly different suggestions). To apply the exact suggestions you reviewed in a dry-run, use `learn --apply-proposal reports/self_improve_TIMESTAMP/proposal.json`, which applies that saved proposal verbatim through the same safety harness with no LLM call.

### Memory log

`TradingMemoryLog` (`tradingagents/agents/utils/memory.py`) stores resolved/pending trading insights at `~/.tradingagents/memory/trading_memory.md`. Agents receive this as `past_context` in the initial graph state so prior lessons inform new analyses. Rotation is controlled by `config["memory_log_max_entries"]` (default: no rotation).

### Persistent state

| What | Location |
|------|----------|
| Trade log | `~/.tradingagents/trades.json` |
| Allocation weights | `~/.tradingagents/allocation_weights.json` |
| Lessons cache | `~/.tradingagents/lessons_cache.md` (+ `lessons_cache_meta.json`) |
| Memory log | `~/.tradingagents/memory/trading_memory.md` |
| LLM / data cache | `~/.tradingagents/cache/` |
| Agent logs | `~/.tradingagents/logs/` |
| Reports | `./reports/` (project root) |

Report folders: `reports/screening_YYYY-MM-DD_TIMESTAMP/TICKER/` for batch runs; `reports/TICKER_YYYYMMDD_HHMMSS/` for individual `analyze` runs.

### CLI web server (`cli/server.py`)

FastAPI + WebSockets server backing the `dashboard` command. The static frontend is at `cli/static/dashboard.html`. The `Run` tab submits analysis jobs that execute remotely via the server.
