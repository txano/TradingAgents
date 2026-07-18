# TradingAgents — System Architecture

This document is the canonical reference for the system's design, data schemas, and
improvement pathways. Keep it updated when adding new features.

---

## 1. What the system does

TradingAgents is a pre-earnings research and trade management framework. Given a set of
tickers reporting earnings, it:

1. Runs a multi-agent LLM pipeline to analyze each ticker
2. Scores each ticker on four buckets (beat / guidance / setup / fundamentals), the
   fundamentals score being grounded in hard yfinance statement metrics
3. Fetches pricing context (spot, valuation, options-implied earnings move) so the
   council can judge what the market has already priced in
4. Uses an AI Council (5 persona advisors + persona-aware cross-review + synthesis,
   followed by a deterministic constraint validator) to allocate a capital budget
   across the screened tickers
5. Imports actual trade results from IBKR and logs them
6. Runs post-trade reflections and distils them into a lessons library that feeds
   back into future allocations
7. Calibrates prediction accuracy against actual earnings outcomes
8. Exposes a web dashboard and CLI stats commands for performance review

---

## 2. Analysis pipeline (per ticker)

```
EarningsLayer
  └── data_fetcher.py       fetch raw EPS estimates, price history, news, peer read-through
  └── peers.py              peer earnings read-through (#9): curated PEER_MAP →
                            EPS surprise + day-1 reaction for peers reported in ~35d,
                            beat-and-still-fall (sector_bar_elevated) flag
  └── analyst.py            LLM generates the pre-earnings brief
  └── scorer.py             extracts beat/guidance/setup scores from the brief

TradingAgentsGraph (LangGraph, 5 teams in sequence)
  Team 1 — Analysts         market, fundamentals, news, social media
  Team 2 — Researchers      bull_researcher, bear_researcher debate
  Team 3 — Research Mgr     research_manager synthesizes the debate
  Team 4 — Risk Mgmt        aggressive / conservative / neutral debators
  Team 5 — Portfolio Mgr    final BUY / SHORT / SKIP decision

AllocationLayer
  └── layer.py                orchestrates council, applies weights, builds ticker
                              contexts (incl. screening history), saves report
  └── council.py              AI Council pipeline (11 LLM calls, 3 rounds + validation)
  └── validator.py            deterministic constraint checks on the allocation JSON
  └── weights.py              load / save / apply the four scoring weights
  └── fundamentals_scorer.py  metrics-grounded LLM fundamentals quality score
  └── pricing.py              spot / valuation / options-implied earnings move
  └── asymmetry.py            payoff asymmetry from historical prints (E[move|beat/miss], fade rate, coverage, EV)
  └── crowding.py             run-up vs sector ETF, 52w-high distance, EPS-revision momentum (#14c)
  └── insider.py              cluster-buy / sell→buy-reversal detection from insider tape (#18)
  └── regime.py               tactical regime gates (#15b): SPX-vs-50dma / VIX risk-off,
                              FOMC/CPI collision calendar, sizing multiplier
  └── common.py               shared helpers (cut, parse_allocation)

LearningLayer (tradingagents/learning/)
  └── lessons.py            distils post-trade reflections into a rules library
                            injected into the council synthesis prompt (cached at
                            ~/.tradingagents/lessons_cache.md, digest-invalidated)
```

Batch screening runs are written under `reports/earnings/` and named
`screening_YYYY-MM-DD_TIMESTAMP` (plain screen) or `earnings_YYYY-MM-DD_TIMESTAMP`
(calendar-driven). `tradingagents/reports_layout.py` is the single source of truth
for this location: `runs_root()` returns the write dir, `iter_run_dirs()` lists all
runs (both prefixes, newest-first, including legacy repo-root runs). Both the CLI
`screen` command and the dashboard server write here via the shared `screen_ticker()`.

Output per ticker (saved to `reports/earnings/{screening,earnings}_YYYY-MM-DD_TIMESTAMP/TICKER/`):

| File | Contents |
|------|----------|
| `earnings_brief.md` | Pre-earnings narrative + JSON score block |
| `earnings_raw_data.json` | Raw EPS estimates and historical data |
| `fundamentals_score.json` | Fundamentals quality score + computed statement metrics |
| `pricing.json` | Spot, market cap, fwd P/E, 52w position, implied earnings move |
| `asymmetry.json` | Historical E[move\|beat], E[move\|miss], fade rate, coverage ratio, EV of a long |
| `crowding.json` | Run-up (1m/3m, vs sector ETF), distance from 52w high, EPS-revision momentum |
| `insider.json` | Insider signal (#18): cluster buys, sell→buy reversals, net buy/sell values, routine-selling flag |
| `peers.json` | Peer earnings read-through (#9): peers reported in last ~35d, EPS surprise + day-1 reaction, `sector_bar_elevated` (beat-and-still-fall) flag |
| `complete_report.md` | Full LangGraph multi-agent report |
| `1_analysts/` … `5_portfolio/` | Per-team agent outputs |

Output per screening run (saved to `reports/earnings/{screening,earnings}_YYYY-MM-DD_TIMESTAMP/`):

| File | Contents |
|------|----------|
| `screening_table.md` | Ranked table of all tickers with scores |
| `allocation.md` | AI Council allocation report |
| `regime.json` | Market regime at allocation time (#15b): SPX vs 50-dma, VIX, risk_off flag |
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

### 3a-bis. Fundamentals score (from `fundamentals_score.json`)

Produced by `allocation/fundamentals_scorer.py`: hard metrics are computed from
yfinance quarterly statements (revenue YoY growth, gross/operating margin deltas,
TTM FCF, debt/EBITDA, cash vs debt) and given to the LLM as ground truth alongside
the fundamentals analyst report.

```json
{
  "fundamentals_score": -5 to +5,  // -5 distressed … +5 fortress
  "balance_sheet": "Strong | Adequate | Weak",
  "profitability": "Expanding | Stable | Contracting",
  "growth_quality": "High | Medium | Low",
  "summary": "one sentence",
  "metrics": { "...computed statement metrics, for traceability..." }
}
```

### 3b. Weighted score (computed by `weights.py`)

```
weighted_score = beat_weight × beat_score
               + guidance_weight × guidance_score
               + setup_weight × setup_score
               + fundamentals_weight × fundamentals_score
```

Weights are stored at `~/.tradingagents/allocation_weights.json`. Defaults (single
source: `weights.DEFAULTS`): beat 0.7, guidance 1.0, setup 1.0, fundamentals 1.5 —
business quality is weighted highest, a single quarter's beat lowest. These are
configured priorities, not calibrated coefficients (see §10).
The weighted_score is passed to the AI Council as the primary sizing signal.

**Score interpretation thresholds used by the council** scale with the weights so
they keep meaning the same thing when weights change. With
`max_score = 5 × Σ weights` (21 at defaults):

| weighted_score | Interpretation | At default weights |
|----------------|----------------|--------------------|
| ≥ 0.50 × max   | Strong long signal | ≥ +11 |
| ≥ 0.25 × max   | Moderate long signal | +5 to +10 |
| below that     | Weak — skip unless brief is compelling | ≤ +4 |
| ≤ −0.25 × max  | Short candidate (High conviction only) | ≤ −5 |

---

## 4. AI Council pipeline (`tradingagents/allocation/council.py`)

Runs 11 LLM calls in 3 parallel rounds, plus a deterministic validation step
(and up to 1 corrective LLM call):

```
Round 1 (parallel × 5): Five advisors each review all tickers
  Alpha   — Contrarian        stress-tests bull cases, finds failure modes
  Beta    — First Principles   rebuilds thesis from scratch, questions assumptions
  Gamma   — Expansionist       finds upside and catalysts others miss
  Delta   — Outsider           pure numbers, no narrative preconceptions
  Epsilon — Executor           blunt: will this make money, how much

Round 2 (parallel × 5): Each advisor cross-reviews the anonymized batch
  THROUGH THEIR OWN PERSONA (one perspective may be their own; judged on merits):
    - Which perspective is strongest?
    - Which has the biggest blind spot?
    - What did all advisors miss?

Round 3 (single): Synthesis (always uses the main deep-thinking LLM)
  Input:  original ticker data (scores, fundamentals, pricing/implied move,
          screening history) + perspectives + cross-reviews + lessons library
  Output: final allocation report (Council Summary + Rationale + table + JSON)

Validation (deterministic, allocation/validator.py):
  parse_allocation → validate_allocation; on violations, ONE corrective re-prompt
  (accepted only if it strictly reduces violations); anything remaining is appended
  to the report as a visible "### ⚠ Constraint Check" section.
```

Robustness behavior:
- Advisors that error out are dropped entirely (never stubbed into prompts); the
  council aborts if fewer than 2 respond.
- Advisors can run on different models via `config["council_advisor_models"]`
  (list of `"provider:model"` strings, env `TRADINGAGENTS_COUNCIL_ADVISOR_MODELS`,
  assigned round-robin). Empty list = all advisors use the main LLM.

Sizing rules enforced in the synthesis prompt AND re-checked by the validator:
- High conviction → 15–25% of budget
- Medium conviction → 7–14%
- Low conviction → ≤ 6% or SKIP
- Single position cap: 30% of budget
- Sector cap: 35% of total budget
- Max 6 positions (BUY + SHORT combined)
- Shorts: High conviction only, weighted_score ≤ −0.25 × max_score (−5 at defaults)
- Budget arithmetic: deployed + cash = budget; amounts match pct_of_budget
- Implied earnings move is treated as the market's priced-in expectation: size
  down when the move is expensive relative to conviction

Implied-move sizing cap & regime gates (#15, in `regime.py` + `validator.py` + synthesis prompt):
- **Implied-move loss cap (#15a, hard):** a position's plausible one-day loss
  (`amount × implied_move`) may not exceed 1% of budget (`IMPLIED_MOVE_LOSS_CAP`) —
  a ±10% implied-move name gets half the dollars of a ±5% name. Applies to BUYs
  and SHORTs; violation → corrective re-prompt → `⚠ Constraint Check`.
- **Tactical regime gates (#15b):** the regime (SPX below its 50-dma or VIX > 22
  → risk-off) is fetched once per allocation run, cached as `regime.json` at the
  run root, and shown to the synthesis LLM as a `=== MARKET REGIME ===` block.
  Risk-off halves the loss cap; an FOMC/CPI release within ±2 sessions of a
  ticker's print (static calendars in `regime.py`, per-ticker `Macro:` line)
  halves it again. `regime.sizing_multiplier` is the single source for the
  multiplier, used by both the prompt and the validator.

Payoff-asymmetry & crowding gates (#14, in `validator.py` + synthesis prompt):
- **Hard EV gate (#14b):** a BUY whose computable expectancy is clearly negative
  (`EV ≤ 0` or `EV/implied_move < 0.25`) is a validation violation → corrective
  re-prompt, then the `⚠ Constraint Check` section. Only longs; SHORTs exempt.
- **Soft sizing advisories (#14b/#14c):** quality names with null EV but high fade
  rate / low coverage, or crowded names (large run-up vs sector, near 52w high,
  cluster of up-revisions), get a non-blocking "size one tier smaller" note in a
  combined `ⓘ Sizing Advisories` section — downgrade, not skip.
- Gate thresholds are tunable constants in `validator.py` / `crowding.py`, to be
  recalibrated by the backtest harness (#17) against the real trade log.

Peer read-through gate (#9, soft, in the synthesis prompt + the `Peers:` ticker line):
- A `⚠ elevated bar` tag (at least one peer **beat and still fell**) → cut conviction
  one tier on longs in that name; peers shown missing (M) on a shared driver → lean
  SKIP over BUY; peers beating and holding → genuine tailwind. Soft, never an auto-skip.

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

### Schema v2 fields (#17 — `tradingagents/trade_log.py`)

`ensure_v2()` stamps `schema_version: 2` and adds the fields below (null until
filled). It runs on every IBKR import; `backfill-trades` populates the rest by
linking each trade to its screening run (`find_screening_run`, matches both
`screening_*` and `earnings_*` run dirs) and reading the saved artifacts +
yfinance. All enrichment is idempotent and guarded — re-running never clobbers
existing values or raises.

| Group | Fields | Source |
|-------|--------|--------|
| T-1 context | `implied_move_pct` | `pricing.json` |
| | `runup_1m_pct`, `runup_vs_sector_1m`, `dist_52w_high_pct`, `revision_direction_30d` | `crowding.json` |
| | `short_interest_pct` | yfinance `info.shortPercentOfFloat` |
| | `iv_rank`, `term_ratio`, `skew_25d` | **null** — owned by #3b (IBKR options) |
| | `regime_flag` | run-root `regime.json` (#15) — "risk_off" / "normal" |
| Outcome | `beat_eps` | yfinance `earnings_dates` |
| | `beat_rev`, `guide` | **null** — not auto-derivable yet |
| Reaction | `move_d1`, `move_d5`, `move_d20` | yfinance price history around the print |
| | `coverage_ratio` | `asymmetry.json` |
| Management | `pnl_final` | mirrors `pnl` on close |
| | `gate_path`, `action` | **null** — owned by #16 |

> Note: historical `reports/earnings/earnings_*` runs predate the
> `pricing/crowding/asymmetry.json` artifacts, so backfill recovers only
> outcome/reaction/short-interest for those trades. Future screens that save the
> artifacts are fully enrichable.

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
├── calibration_master.json         ← aggregated calibration across all runs (repo root)
├── calibration_master.md
├── earnings/                        ← canonical location for all batch screening runs
│   └── {screening,earnings}_YYYY-MM-DD_TIMESTAMP/ ← one folder per screen run
│       ├── screening_table.md      ← ranked tickers table
│       ├── allocation.md           ← AI Council allocation report
│       ├── metadata.json           ← run_type / models / run_at (dashboard runs)
│       ├── calibration.json        ← post-earnings accuracy (written by calibrate)
│       ├── calibration.md
│       └── TICKER/
│           ├── earnings_brief.md       ← pre-earnings brief + score JSON
│           ├── earnings_raw_data.json
│           ├── fundamentals_score.json ← fundamentals quality score + metrics
│           ├── pricing.json            ← spot / valuation / implied earnings move
│           ├── asymmetry.json          ← historical payoff asymmetry + EV
│           ├── crowding.json           ← run-up / 52w-high / revision momentum
│           ├── insider.json            ← cluster buys / sell→buy reversals (#18)
│           ├── peers.json              ← peer earnings read-through (#9)
│           ├── complete_report.md
│           └── 1_analysts/ … 5_portfolio/
├── analysis/TICKER_YYYYMMDD_HHMMSS/ ← individual analyze runs (not from screen)
└── reflections/
    └── TICKER_YYYYMMDD_HHMMSS/     ← post-trade reflection outputs
```

---

## 9. CLI commands

| Command | What it does |
|---------|-------------|
| `analyze` | Run full LangGraph pipeline on a single ticker |
| `screen` | Run EarningsLayer + pipeline on a batch of tickers, then allocate |
| `earnings-calendar` | Fetch upcoming earnings and feed them into a screen |
| `allocate` | Rebuild screening_table + re-run AI Council on an existing screening dir |
| `reflect` | Post-trade reflection for a completed trade (interactive, pick trades) |
| `learn` | Reflect on ALL trades, analyse them, and auto-apply weight + prompt improvements (non-interactive) |
| `improve` | LLM analysis of past reflections → report only (interactive) |
| `trades` | Display trade history table |
| `calibrate` | Measure prediction accuracy for a past screening run |
| `correlation` | Score-to-outcome correlation analysis |
| `stats` | Win rate, total P&L, Sharpe/Sortino/max-drawdown (per-trade), beat/guidance accuracy, calibration by confidence |
| `import-ibkr` | Import closed trades from IBKR Flex XML |
| `backfill-trades` | Backfill schema-v2 context/outcome/reaction fields on `trades.json` (#17; `--no-network`, `--ticker`) |
| `allocation-weights` | View or update the four scoring weights (beat / guidance / setup / fundamentals) |
| `dashboard` | Launch local web dashboard (http://127.0.0.1:8765) |
| `build-web` | Build the static reports website |

Subcommand implementations live in `cli/commands/`; `cli/main.py` registers them and
hosts the interactive menu (shown when no subcommand is given) plus the `analyze` flow.

**Shared screening primitives (use these instead of re-implementing — the CLI `screen`/`allocate`
commands and the dashboard `server.py` all call them, so they can't drift):**
`screen_ticker()` and `run_allocation()` in `cli/commands/screen.py` (per-ticker run + the AI-Council
invocation), `write_screening_table()` in `tradingagents/screening_table.py` (the one screening_table.md
writer, kept in sync with `calibrator.parse_screening_table`), and `_fetch_sector()` / `gather_api_keys()`
in `cli/commands/common.py`. Run locations resolve through `tradingagents/reports_layout.py`.

---

## 10. Improving weights from calibration and reflections

This is the intended feedback loop for tuning the system over time.

### Lessons feedback loop (already automated)

`tradingagents reflect` writes `post_mortem.md` files under `reports/reflections/`.
At allocation time, `learning/lessons.py` distils them into a rules library (one
LLM call) that is injected into the council synthesis prompt. Rules require ≥2
supporting trades and state their evidence count `(n=X)`; single-trade
observations are listed as tentative "Patterns to Watch". The result is cached at
`~/.tradingagents/lessons_cache.md` and re-distilled only when the reflection
files change (digest of paths + mtimes + sizes).

### Automated `learn` loop (reflect-all → analyse → apply)

`tradingagents learn` is the non-interactive, scheduler-friendly version of the
whole loop (`learning/trade_reflections.py` + `learning/self_improve.py`):

1. **Reflect on all trades.** `reflect_all()` consolidates fills by ticker+exit_date
   and runs `ReflectionLayer` over every trade in `trades.json`. Pending-only by
   default (idempotent — safe to re-run); `--force` re-reflects everything.
2. **Analyse.** One deep-think call over all reflections returns a markdown report
   plus a structured JSON proposal: `weights`, `prompt_edits`, `process_notes`.
3. **Apply.**
   - *Weights* — applied via `save_weights()`, clamped to [0, 3]. Deterministic, reversible.
   - *Prompt edits* — applied to an allowlist of agent-prompt files
     (`EDITABLE_PROMPT_FILES`) under a strict harness: target must be on the
     allowlist and inside the repo; `old_string` must match exactly once; f-string
     `{placeholder}` tokens and brace balance must be preserved; every file is
     backed up first; and a post-write `compile()` check restores the backup on any
     syntax break. Rejected edits are logged, never force-applied.
   - *Process notes* are recorded only (structural ideas a human/agent acts on).

Output lands in `reports/self_improve_TIMESTAMP/` (`improvement_report.md`,
`proposal.json`, `CHANGELOG.md`, `backups/`). Flags: `--dry-run` (apply nothing),
`--skip-reflect`, `--no-weights`, `--no-prompt-edits`. All applied changes are
git-tracked, so `git diff` / `git checkout` is the universal undo. This is the
seed of the #13 "dream mode" idle loop on the roadmap.

### What weights do

```
weighted_score = beat_w × beat_score + guidance_w × guidance_score
               + setup_w × setup_score + fundamentals_w × fundamentals_score
```

A weight of 1.5 for `fundamentals` means the council treats business quality as 50%
more significant when sizing positions. A weight of 0.7 for `beat` means a single
quarter's beat expectation is trusted 30% less. The council's signal thresholds
scale with the weights automatically (§3b), so adjusting weights does not break
the score interpretation.

### Where weights live

`~/.tradingagents/allocation_weights.json` (defaults from `weights.DEFAULTS`):
```json
{ "beat": 0.7, "guidance": 1.0, "setup": 1.0, "fundamentals": 1.5 }
```

### How to update weights from calibration data

1. Run `tradingagents calibrate` after earnings announcements for several runs.
2. Read `reports/calibration_master.json` — look at per-bucket accuracy.
3. The `rows[]` array has `beat_prediction_correct` and `signal_correct` for each ticker.
   You can compute bucket-level accuracy by correlating each score with outcomes:
   - High `beat_score` + `beat_prediction_correct=true` → beat bucket is trustworthy → increase `beat_w`
   - High `guidance_score` but `signal_correct` frequently false → guidance is noisy → decrease `guidance_w`
4. Apply new weights: `tradingagents allocation-weights --beat 1.4 --guidance 0.8 --fundamentals 1.2`

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
| Lessons cache | `~/.tradingagents/lessons_cache.md` (+ `lessons_cache_meta.json`) |
| Cache | `~/.tradingagents/cache/` |
| Agent logs | `~/.tradingagents/logs/` |
| Reports | `./reports/` (relative to project root) |
| Council logic | `tradingagents/allocation/council.py` |
| Allocation validator | `tradingagents/allocation/validator.py` |
| Weights logic | `tradingagents/allocation/weights.py` |
| Fundamentals scorer | `tradingagents/allocation/fundamentals_scorer.py` |
| Pricing / implied move | `tradingagents/allocation/pricing.py` |
| Payoff asymmetry / EV | `tradingagents/allocation/asymmetry.py` |
| Crowding / run-up gate | `tradingagents/allocation/crowding.py` |
| Insider signal | `tradingagents/allocation/insider.py` |
| Regime gates / macro calendar | `tradingagents/allocation/regime.py` |
| Lessons library | `tradingagents/learning/lessons.py` |
| Batch reflection | `tradingagents/learning/trade_reflections.py` |
| Self-improvement engine | `tradingagents/learning/self_improve.py` (weight + guarded prompt-edit auto-apply) |
| Calibration logic | `tradingagents/calibration/calibrator.py` |
| IBKR import | `tradingagents/ibkr/flex_client.py` |
| CLI entry point | `cli/main.py` (subcommands in `cli/commands/`) |
| Dashboard HTML | `cli/static/dashboard.html` |
| Default LLM config | `tradingagents/default_config.py` |
