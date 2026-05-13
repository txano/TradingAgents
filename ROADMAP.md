# TradingAgents — Roadmap

Personal pre-earnings trading system built on top of TradingAgents.
This file tracks the planned improvements in priority order.

---

## Status legend
- `[ ]` Not started
- `[~]` In progress
- `[x]` Done

---

## #1 — Calibration & Accuracy Tracking

**Goal:** Close the feedback loop. After earnings are announced, automatically score each prediction against reality and surface accuracy metrics over time.

### Sub-tasks
- [x] Weekly calibration job: `uv run tradingagents calibrate` — scans existing `screening_table.md` files, fetches actual EPS/revenue results from yfinance, and compares against `beat_score` and `guidance_score` predictions
- [x] Store calibration results alongside each screening run (`calibration.json` + `calibration.md` in the screening folder)
- [x] `uv run tradingagents stats` command: reads `trades.json` + calibration results and prints:
  - Signal accuracy rate (BUY/SHORT/SKIP — how often were we right?)
  - Score-bucket accuracy: does total_score +8 really mean 80% hit rate?
  - Confidence calibration: are "High" calls actually more accurate?
  - Beat / guidance prediction accuracy separately
- [x] Link each trade in `trades.json` to the screening run it came from (add `screening_run` field)

### Notes
- `reflect` command + `trades.json` already capture trade outcomes; this builds the prediction side on top
- Priority: run after a few weeks of data accumulate to have a meaningful sample

---

## #2 — Investment Tracking

**Goal:** Single source of truth for what was traded, what happened, and what was made.

### Sub-tasks
- [x] `trades.json` — logs each trade (ticker, direction, shares, entry/exit price, P&L, dates)
- [x] `uv run tradingagents reflect` — post-mortem on a completed trade, writes to `trades.json`
- [x] `uv run tradingagents trades` command: display full trade history as a table (P&L summary, win rate, avg return per trade)
- [x] Add `screening_run` field to `trades.json` to link trade → original prediction
- [ ] Track unrealised vs. realised P&L (optional, needs current price feed)

### Notes
- The reflect command produces post-mortems in `reports/reflections/` — these are the qualitative record
- `trades.json` at `~/.tradingagents/trades.json` is the quantitative record

---

## #3 — Additional Data Sources (IBKR + Indicator Improvements) ← NEXT

**Goal:** Improve signal quality, especially `setup_score`, by feeding in data the current system is blind to.

### 3a — Technical indicator improvements (quick win, no new integrations)
- [x] Add `atr` (ATR-14) officially to the market analyst prompt — measures expected daily move, key for sizing
- [x] Add `adx` (ADX-14) — trend strength (not direction); helps distinguish trending vs. choppy setups
- [x] Add `cci` (CCI-20) — cyclical momentum; complements RSI in detecting overbought conditions pre-earnings
- All three already work in stockstats — just need to be added to `market_analyst.py`

### 3b — Interactive Brokers data integration
- [ ] Set up `ib_insync` connection to IBKR TWS/Gateway
- [ ] Fetch **Implied Volatility** for front-month options (most important: tells you what the market is pricing in for the earnings move)
- [ ] Compute **Expected Move** = stock price × IV × √(days_to_earnings / 365)
- [ ] Fetch **IV Rank / IV Percentile** (current IV vs. 52-week range) — high IVR = expensive options, harder to profit directionally
- [ ] Fetch **Put/Call Open Interest Ratio** at nearest strikes — skew indicator
- [ ] Feed these into the earnings brief prompt as a new `IBKR DATA` section
- [ ] Update `setup_score` scoring guide to incorporate IV data (e.g., heavily penalise setups where expected move already exceeds our predicted move)

### Notes
- Items in 3b require IBKR TWS/Gateway running locally
- IV + Expected Move are the highest-priority additions for this specific use case — without them, a "BUY" call ignores whether the move is already priced in
- Library: `ib_insync` (pip install ib_insync) — simpler than native ibapi

---

## #4 — Sector Tracking & Memory

**Goal:** Build sector-level pattern recognition on top of per-stock analysis.

### Phase A — Sector column (quick win)
- [x] Add `sector` field to screening results (fetch from `yfinance.Ticker.info["sector"]`)
- [x] Show sector column in the screening table and allocation report
- [ ] Track sector concentration in the allocation manager (flag if >40% in one sector)

### Phase B — Sector memory
- [ ] Create `memory/sectors/` folder structure — one file per sector (Technology, Healthcare, etc.)
- [ ] After each screening + calibration cycle, append: what we predicted, what happened, pattern notes
- [ ] Feed relevant sector memory into the earnings brief prompt as additional context
- [ ] Track sector-level accuracy separately in `tradingagents stats`

### Phase C — Sector news feed (longer term)
- [ ] Daily/weekly sector news ingestion (yfinance or news API)
- [ ] LLM summarisation per sector, stored in sector memory files
- [ ] Surface in earnings briefs for companies in that sector

---

## #5 — Options Strategy Research

**Goal:** Evaluate whether vertical spreads (or other defined-risk structures) offer better risk/reward than outright stock positions for pre-earnings plays.

### Research questions to answer
- [ ] For our historical trades: what would the P&L have been with a vertical spread vs. outright position?
- [ ] At what IV Rank level does buying options become too expensive (IV crush risk)?
- [ ] Which spread structure fits this strategy: bull call spread (BUY), bear put spread (SHORT), or iron condor (SKIP with high uncertainty)?
- [ ] What is the practical max-loss per trade with defined-risk structures vs. current approach?
- [ ] Does IBKR support programmatic options order placement for this workflow?

### Notes
- This item is a **research + decision task** before any implementation
- Depends on #3b (IV data) — need IV to evaluate options pricing
- Depends on #2 (trade history) — need enough trades to backtest spread P&L
- Recommended: revisit after 2–3 months of live trades and #3b is in place

---

## Quick wins (no dedicated section above)

- [x] Add `--budget` flag to `tradingagents screen` so the $100k allocation budget is configurable at runtime
- [x] Clean up `screen.py` root script — updated to DeepSeek V4
- [x] Add sector to `trades.json` entries for future sector-level P&L analysis

---

*Last updated: 2026-04-27*
