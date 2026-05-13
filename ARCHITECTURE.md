# TradingAgents — System Architecture

This document is the canonical reference for the system's design, data schemas, and
improvement pathways. Keep it updated when adding new features.

---

## 1. What the system does

TradingAgents is a pre-earnings research and trade management framework. Given a set of
tickers reporting earnings, it:

1. Runs a multi-agent LLM pipeline to analyze each ticker
2. Scores each ticker on three buckets (beat / guidance / setup)
3. Uses an AI Council (5 advisors + cross-review + synthesis) to allocate a capital
   budget across the screened tickers
4. Imports actual trade results from IBKR and logs them
5. Runs post-trade reflections to capture lessons
6. Calibrates prediction accuracy against actual earnings outcomes
7. Exposes a web dashboard and CLI stats commands for performance review

---

## 2. Analysis pipeline (per ticker)

```
EarningsLayer
  └── data_fetcher.py       fetch raw EPS estimates, price history, news
  └── analyst.py            LLM generates the pre-earnings brief
  └── scorer.py             extracts beat/guidance/setup scores from the brief

TradingAgentsGraph (LangGraph, 5 teams in sequence)
  Team 1 — Analysts         market, fundamentals, news, social media
  Team 2 — Researchers      bull_researcher, bear_researcher debate
  Team 3 — Research Mgr     research_manager synthesizes the debate
  Team 4 — Risk Mgmt        aggressive / conservative / neutral debators
  Team 5 — Portfolio Mgr    final BUY / SHORT / SKIP decision

AllocationLayer
  └── layer.py              orchestrates council, applies weights, saves report
  └── council.py            AI Council pipeline (11 LLM calls, 3 rounds)
  └── weights.py            load / save / apply scoring weights
```

Output per ticker (saved to `reports/screening_YYYY-MM-DD_TIMESTAMP/TICKER/`):

| File | Contents |
|------|----------|
| `earnings_brief.md` | Pre-earnings narrative + JSON score block |
| `earnings_raw_data.json` | Raw EPS estimates and historical data |
| `complete_report.md` | Full LangGraph multi-agent report |
| `1_analysts/` … `5_portfolio/` | Per-team agent outputs |

Output per screening run (saved to `reports/screening_YYYY-MM-DD_TIMESTAMP/`):

| File | Contents |
|------|----------|
| `screening_table.md` | Ranked table of all tickers with scores |
| `allocation.md` | AI Council allocation report |
| `calibration.json` | Post-earnings accuracy measurement (written by `calibrate`) |
| `calibration.md` | Human-readable calibration report |

---

## 3. Scoring schema

### 3a. Per-ticker scores (from `earnings_brief.md` JSON block)

```json
{
  "earnings_date": "YYYY-MM-DD",
  "beat_score":     -5 to +5,   // EPS beat likelihood
  "guidance_score": -5 to +5,   // Forward guidance tone
  "setup_score":    -5 to +5,   // Technical / fundamental pre-earnings setup
  "total_score":    -15 to +15, // raw sum: beat + guidance + setup
  "signal":         "BUY | SHORT | SKIP",
  "confidence":     "High | Medium | Low",
  "one_liner":      "string"
}
```

### 3b. Weighted score (computed by `weights.py`)

```
weighted_score = beat_weight × beat_score
               + guidance_weight × guidance_score
               + setup_weight × setup_score
```

Weights are stored at `~/.tradingagents/allocation_weights.json` (default: all 1.0).
The weighted_score is passed to the AI Council as the primary sizing signal.

**Score interpretation thresholds used by the council:**

| weighted_score | Interpretation |
|----------------|----------------|
| ≥ +8           | Strong long signal |
| +4 to +7       | Moderate long signal |
| ≤ +3           | Weak — skip unless brief is compelling |
| ≤ -3           | Short candidate (High conviction only) |

---

## 4. AI Council pipeline (`tradingagents/allocation/council.py`)

Runs 11 LLM calls in 3 parallel rounds:

```
Round 1 (parallel × 5): Five advisors each review all tickers
  Alpha   — Contrarian        stress-tests bull cases, finds failure modes
  Beta    — First Principles   rebuilds thesis from scratch, questions assumptions
  Gamma   — Expansionist       finds upside and catalysts others miss
  Delta   — Outsider           pure numbers, no narrative preconceptions
  Epsilon — Executor           blunt: will this make money, how much

Round 2 (parallel × 5): Each advisor cross-reviews the anonymized batch
  Per reviewer:
    - Which perspective is strongest?
    - Which has the biggest blind spot?
    - What did all five miss?

Round 3 (single): Synthesis
  Input:  original ticker data + 5 perspectives + 5 cross-reviews
  Output: final allocation report (Council Summary + Rationale + table + JSON)
```

Sizing rules enforced in the synthesis prompt:
- High conviction → 15–25% of budget
- Medium conviction → 7–14%
- Low conviction → ≤ 6% or SKIP
- Single position cap: 30% of budget
- Sector cap: 35% of deployed capital
- Shorts: High conviction only, weighted_score ≤ -3

---

## 5. Calibration system (`tradingagents/calibration/calibrator.py`)

After earnings are announced, `tradingagents calibrate` measures prediction accuracy:

1. Parses `screening_table.md` to get our predictions
2. Fetches actual EPS results and price action from yfinance
3. Computes:
   - `beat_prediction_correct`: did we predict the EPS beat direction correctly?
   - `signal_correct`: did the price move in the direction of our signal?
4. Writes `calibration.json` and `calibration.md` to the screening dir
5. Rebuilds `reports/calibration_master.json` and `calibration_master.md`

### Calibration JSON schema (`calibration.json` → `rows[]`)

```json
{
  "ticker": "AAPL",
  "earnings_date": "YYYY-MM-DD",
  "beat_score": 3,
  "guidance_score": 2,
  "setup_score": 2,
  "total_score": 7,
  "signal": "BUY",
  "confidence": "High",
  "reported_eps": 1.52,
  "estimated_eps": 1.43,
  "actual_beat": true,
  "surprise_pct": 6.3,
  "price_change_pct": 4.2,
  "beat_prediction_correct": true,
  "signal_correct": true
}
```

---

## 6. Trade log (`~/.tradingagents/trades.json`)

Each entry is one closed trade. Fields:

| Field | Type | Source | Notes |
|-------|------|--------|-------|
| `ticker` | str | all | |
| `sector` | str | yfinance | fetched at analysis time |
| `direction` | str | agent | BUY or SHORT |
| `shares` | float | IBKR / manual | |
| `entry_price` | float | IBKR / manual | reconstructed from Flex close record |
| `exit_price` | float | IBKR / manual | |
| `pnl` | float | IBKR | net P&L after commission |
| `pnl_pct` | float | computed | pnl / (shares × entry_price) × 100 |
| `outcome` | str | computed | WIN / LOSS / BREAK_EVEN |
| `beat_prediction_correct` | bool\|null | reflect | set after running `reflect` |
| `guidance_prediction_correct` | bool\|null | reflect | set after running `reflect` |
| `key_lesson` | str | reflect | free-text lesson from reflection |
| `trade_date` | str\|null | manual | entry date (not available from Flex) |
| `exit_date` | str | IBKR | YYYY-MM-DD |
| `screening_run` | str\|null | reflect | screening dir name |
| `analysis_path` | str\|null | reflect | path to ticker analysis folder |
| `reflection_path` | str\|null | reflect | path to reflection output |
| `source` | str | | "ibkr" or "manual" |
| `currency` | str | IBKR | |
| `ibkr_trade_id` | str\|null | IBKR | used for deduplication on re-import |
| `ibkr_exec_id` | str\|null | IBKR | |
| `logged_at` | str | system | ISO timestamp |

---

## 7. IBKR import (`tradingagents/ibkr/flex_client.py`)

- Uses IBKR Flex Web Service (REST, two-step: SendRequest → poll GetStatement)
- Parses XML: filters `assetCategory == STK` and `openCloseIndicator == C`
- Reconstructs `entry_price` from exit price and `fifoPnlRealized`:
  - BUY (closed by SELL): `entry = exit - pnl_gross / qty`
  - SHORT (closed by BUY): `entry = exit + pnl_gross / qty`
- `net_pnl = pnl_gross - ibCommission`
- Import filter (default, bypassed with `--all`):
  - Ticker must exist in a `reports/screening_*/TICKER/` or `reports/TICKER_YYYYMMDD_*/` folder
  - Trade `exit_date` must be ≥ the earliest analysis date for that ticker

---

## 8. Reports folder layout

```
reports/
├── calibration_master.json         ← aggregated calibration across all runs
├── calibration_master.md
├── screening_YYYY-MM-DD_TIMESTAMP/ ← one folder per screen run
│   ├── screening_table.md          ← ranked tickers table
│   ├── allocation.md               ← AI Council allocation report
│   ├── calibration.json            ← post-earnings accuracy (written by calibrate)
│   ├── calibration.md
│   └── TICKER/
│       ├── earnings_brief.md       ← pre-earnings brief + score JSON
│       ├── earnings_raw_data.json
│       ├── complete_report.md
│       └── 1_analysts/ … 5_portfolio/
├── TICKER_YYYYMMDD_HHMMSS/         ← individual analyze runs (not from screen)
└── reflections/
    └── TICKER_YYYYMMDD_HHMMSS/     ← post-trade reflection outputs
```

---

## 9. CLI commands

| Command | What it does |
|---------|-------------|
| `analyze` | Run full LangGraph pipeline on a single ticker |
| `screen` | Run EarningsLayer + pipeline on a batch of tickers, then allocate |
| `allocate` | Rebuild screening_table + re-run AI Council on an existing screening dir |
| `reflect` | Post-trade reflection for a completed trade |
| `trades` | Display trade history table |
| `calibrate` | Measure prediction accuracy for a past screening run |
| `stats` | Win rate, avg P&L, beat/guidance accuracy, calibration by confidence |
| `import-ibkr` | Import closed trades from IBKR Flex XML |
| `allocation-weights` | View or update scoring weights (beat / guidance / setup) |
| `dashboard` | Launch local web dashboard (http://127.0.0.1:8765) |

---

## 10. Improving weights from calibration and reflections

This is the intended feedback loop for tuning the system over time.

### What weights do

```
weighted_score = beat_w × beat_score + guidance_w × guidance_score + setup_w × setup_score
```

A weight of 1.5 for `beat` means the council treats beat predictions as 50% more
significant when sizing positions. A weight of 0.7 for `setup` means technical setup
is trusted 30% less.

### Where weights live

`~/.tradingagents/allocation_weights.json`:
```json
{ "beat": 1.0, "guidance": 1.0, "setup": 1.0 }
```

### How to update weights from calibration data

1. Run `tradingagents calibrate` after earnings announcements for several runs.
2. Read `reports/calibration_master.json` — look at per-bucket accuracy.
3. The `rows[]` array has `beat_prediction_correct` and `signal_correct` for each ticker.
   You can compute bucket-level accuracy by correlating each score with outcomes:
   - High `beat_score` + `beat_prediction_correct=true` → beat bucket is trustworthy → increase `beat_w`
   - High `guidance_score` but `signal_correct` frequently false → guidance is noisy → decrease `guidance_w`
4. Apply new weights: `tradingagents allocation-weights --beat 1.4 --guidance 0.8`

### How to update weights from reflections

Each trade in `trades.json` has:
- `beat_prediction_correct` and `guidance_prediction_correct` (from `reflect`)
- `pnl` and `outcome`

To compute which bucket drove profitable trades:

```python
import json
from pathlib import Path

trades = json.loads(Path.home().joinpath(".tradingagents/trades.json").read_text())

# Trades where we made money AND beat prediction was correct
beat_useful = [t for t in trades if t.get("beat_prediction_correct") and t.get("pnl", 0) > 0]
# Trades where beat prediction was correct but we still lost
beat_misleading = [t for t in trades if t.get("beat_prediction_correct") and t.get("pnl", 0) < 0]
```

The ratio `len(beat_useful) / (len(beat_useful) + len(beat_misleading))` gives a rough
signal accuracy for the beat bucket. Do the same for guidance and setup, then normalize
to weights proportionally.

### Planned improvement: automated weight suggestion command

A future `tradingagents suggest-weights` command should:
1. Load all calibration rows from `calibration_master.json`
2. Load all trades with reflection data from `trades.json`
3. Compute per-bucket signal accuracy
4. Propose new weights based on relative accuracy
5. Let the user confirm before writing to `allocation_weights.json`

The key file to implement this is `tradingagents/allocation/weights.py` —
add a `suggest_weights(calibration_rows, trade_entries)` function there.

---

## 11. Key file locations

| What | Path |
|------|------|
| Trade log | `~/.tradingagents/trades.json` |
| Allocation weights | `~/.tradingagents/allocation_weights.json` |
| Cache | `~/.tradingagents/cache/` |
| Agent logs | `~/.tradingagents/logs/` |
| Reports | `./reports/` (relative to project root) |
| Council logic | `tradingagents/allocation/council.py` |
| Weights logic | `tradingagents/allocation/weights.py` |
| Calibration logic | `tradingagents/calibration/calibrator.py` |
| IBKR import | `tradingagents/ibkr/flex_client.py` |
| CLI entry point | `cli/main.py` |
| Dashboard HTML | `cli/static/dashboard.html` |
| Default LLM config | `tradingagents/default_config.py` |
