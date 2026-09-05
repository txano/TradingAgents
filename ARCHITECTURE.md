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
  └── crowding.py             run-up (1w/1m/3m) + sector flow, 52w-high distance, EPS-revision momentum (#14c)
  └── insider.py              cluster-buy / sell→buy-reversal detection from insider tape (#18)
  └── regime.py               tactical regime gates (#15b): SPX-vs-50dma / VIX risk-off,
                              FOMC/CPI collision calendar, sizing multiplier
  └── watchlist.py            wait-and-decide entries (#19): WATCH rows → watchlist.json,
                              status lifecycle (PENDING/ARMED/TRIGGERED/EXPIRED),
                              live quotes + purchased/dismissed user overlay
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
| `crowding.json` | Run-up (1w/1m/3m, vs sector ETF), 1w sector flow, distance from 52w high, EPS-revision momentum |
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
| `watchlist.json` | WATCH entries (#19): trigger price, reserved amount, watch window |
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
- Every council LLM call **streams** its response (non-streaming requests on
  long reasoning-model calls sit silent until completion and get dropped by
  read timeouts as "Connection error"), falling back to a plain invoke when a
  provider can't stream. Transient failures (connection/timeout/5xx) retry up
  to 3 attempts with exponential backoff; 4xx-style errors fail fast. Retries
  (and the final failure) are logged with the real underlying cause (e.g. a
  chained `ReadTimeout`/`RemoteProtocolError`), not just the generic
  "Connection error." string that providers like DeepSeek collapse everything
  to, and surface on the job's progress log, not just the Python logger.
- Batches above ~25 tickers use **condensed ticker sections for the synthesis
  prompt** (per-ticker PM decision + brief bodies omitted — the advisor
  perspectives in the same prompt already digest them), keeping 40+-ticker
  requests inside model context. Advisors always get the full sections.
- The DeepSeek client's default request timeout is 1800s (reasoning models on
  large council prompts — e.g. a 90+-ticker synthesis — legitimately run for
  many minutes; this was raised from 600s after a 99-ticker run still hit
  read-timeout "Connection error"s at that ceiling), and its default
  `max_retries` is 5 rather than the SDK's 2 (see "DeepSeek dropped
  connections" below).
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

Wait-and-decide WATCH outcome (#19, in `watchlist.py` + `validator.py` + synthesis prompt):
- The council may mark a **quality** name (`fundamentals_score ≥ +2`) WATCH instead
  of BUY/SKIP when the pre-print entry is unattractive (EV n/a with high fade/low
  coverage, crowding, Low/Medium confidence): $0 allocated now, a `watch_amount`
  reserved from cash, and a `trigger_price` ≥ 0.5 × the implied move below spot.
  If the stock trades at/below the trigger within `watch_expiry_sessions` (2–5)
  after the print, it is a buy-the-dip candidate with the same thesis.
- Validator checks WATCH rows structurally (zero amount, positive trigger/reserve,
  quality gate, trigger depth, Σ reservations ≤ cash); WATCH rows are exempt from
  the 6-position cap. Entries persist as `watchlist.json` at the run root; the
  dashboard tracks the PENDING → ARMED → TRIGGERED → EXPIRED lifecycle
  (hit = any session low ≤ trigger inside the window). Flag-only: orders are
  placed manually. Trades in WATCH-listed names get `strategy: "wait_and_decide"`.
- **Live quote (`fetch_live_price`):** the dashboard's `Now` column is the last
  traded price (yfinance `fast_info`, 1-day close fallback), TTL-cached 60s
  because the view polls; `price_source` tells the UI whether it got `live` or
  the daily `close`. Trigger *detection* still keys off daily bars — an intraday
  low is what proves a level traded — but an ARMED entry quoting at/below its
  trigger is upgraded to TRIGGERED immediately rather than waiting for the bar
  to settle. Every fetch is guarded; no quote just degrades to the close.
- **User overlay (`purchased` / `dismissed`):** because execution is manual, the
  dashboard needs to record what the user did. `set_entry_state()` writes
  `reports/watchlist_state.json` — keyed `"<run_id>|<TICKER>"`, holding state,
  UTC timestamp, and the quote at purchase time — and `collect_watchlists()`
  merges it onto every entry as `user_state`/`user_state_at`/`user_price`. It
  lives in `reports/` (not the run dir) so run artifacts stay exactly as the
  council produced them, the overlay survives re-runs, and Syncthing carries it
  Mac ↔ Pi alongside `trades.json`. Purchased/dismissed rows drop out of the nav
  badge and the Overview alert; dismissed ones also skip the price fetch. Both
  are reversible (`state=None`), so nothing is destroyed.

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

**1-week run-up & sector flow (#14c, added 2026-08-30).** `crowding.py` also records
`runup_1w_pct` (the name's move over the 5 sessions into the print), a −2..+2
contrarian `runup_1w_score` off it, and the sector's own week (`sector_1w_pct`,
`sector_vs_spy_1w`, using the existing `SECTOR_ETF` map). Measured over 1,128 screened
prints (Jul–Aug 2026) the 1-week window is the sharpest predictor available:
`corr(runup_1w, day+10) = −0.157`, against +0.009 / −0.004 / −0.053 for the beat /
guidance / setup scores. It is **displayed, not scored into `weighted_score`** — it
shows on the council's `Crowding:` line, raises a flag above +5%, and lands in
`crowding.json` and the trade log for #17 analysis. Folding it into the weighted total
is a deliberate decision left open; the bands come from one earnings season.
`RUNUP_1W_BANDS` is the single place to recalibrate.

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

## 6. Trade log (`reports/trades.json`)

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
| | `runup_1w_pct`, `runup_1w_score`, `sector_1w_pct`, `sector_vs_spy_1w`, `runup_1m_pct`, `runup_vs_sector_1m`, `dist_52w_high_pct`, `revision_direction_30d` | `crowding.json` |
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
│           ├── crowding.json           ← run-up (1w/1m/3m) / sector flow / 52w-high / revision momentum
│           ├── insider.json            ← cluster buys / sell→buy reversals (#18)
│           ├── peers.json              ← peer earnings read-through (#9)
│           ├── complete_report.md
│           └── 1_analysts/ … 5_portfolio/
├── analysis/TICKER_YYYYMMDD_HHMMSS/ ← individual analyze runs (not from screen)
└── reflections/
    └── TICKER_YYYYMMDD_HHMMSS/     ← post-trade reflection outputs
```

Individual `analysis/` runs are surfaced on the dashboard's **Analyses** view
alongside every screened ticker (see §12), so a one-off `analyze` isn't stranded
in a folder no page links to.

---

## 9. CLI commands

| Command | What it does |
|---------|-------------|
| `analyze` | Run full LangGraph pipeline on a single ticker |
| `screen` | Run EarningsLayer + pipeline on a batch of tickers, then allocate |
| `earnings-calendar` | Fetch upcoming earnings and feed them into a screen |
| `allocate` | Rebuild screening_table + re-run AI Council on an existing screening dir |
| `resume` | Finish an interrupted run: screen only the missing tickers into the same folder, rebuild the table, allocate (`--dir`, `--workers`, `--no-allocate`, `-y`) |
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

trades = json.loads(Path("reports/trades.json").read_text())   # see _trades_path()

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
| Trade log | `reports/trades.json` (legacy `~/.tradingagents/trades.json` is a stale pre-migration copy) |
| Watchlist user state (#19) | `reports/watchlist_state.json` (purchased / dismissed marks) |
| Job registry | `reports/jobs.json` (durable job list behind the activity banner) |
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
| Wait-and-decide watchlist | `tradingagents/allocation/watchlist.py` |
| Resume an interrupted run | `cli/commands/resume.py` |
| Job registry | `cli/jobs_registry.py` |
| Lessons library | `tradingagents/learning/lessons.py` |
| Batch reflection | `tradingagents/learning/trade_reflections.py` |
| Self-improvement engine | `tradingagents/learning/self_improve.py` (weight + guarded prompt-edit auto-apply) |
| Calibration logic | `tradingagents/calibration/calibrator.py` |
| IBKR import | `tradingagents/ibkr/flex_client.py` |
| CLI entry point | `cli/main.py` (subcommands in `cli/commands/`) |
| Dashboard SPA (served by `cli/server.py`) | `cli/static/reports_site.html` |
| Legacy dashboard HTML | `cli/static/dashboard.html` |
| Default LLM config | `tradingagents/default_config.py` |

---

## 12. DeepSeek dropped connections ("Connection error.")

Every transport-layer fault reaches the app as the same opaque string, because
the openai SDK's request loop ends in `except Exception as err: raise
APIConnectionError(request=request) from err`. A DNS failure, a TLS reset, a
read timeout and a mid-response disconnect are indistinguishable from the
message alone — the real fault is only in the chained `__cause__`.
`tradingagents/llm_clients/errors.py::describe_exc` walks that chain and is what
`screen_ticker` and the council use instead of `str(exc)`; `screen_ticker` also
`logger.exception`s the full traceback before collapsing to a one-liner.

The fault actually seen on batch screens is:

```
openai.APIConnectionError: Connection error.
 <= httpx.RemoteProtocolError: peer closed connection without sending
    complete message body (incomplete chunked read)
```

DeepSeek begins streaming a chunked response and then drops the connection
before finishing. It is **not** a timeout (a read timeout raises
`APITimeoutError: Request timed out.` instead), and **not** a concurrency
problem — it reproduces on a single ticker with no parallelism.

**It is one model, and only when not streaming.** DeepSeek holds a slow request
open by sending bare empty lines on the non-streaming path, but proper
`: keep-alive` SSE comments when streaming
([rate-limit docs](https://api-docs.deepseek.com/quick_start/rate_limit)). The
empty-line path does not survive long `v4-pro` generations. Controlled A/B
(2026-08-01), identical prompt, fresh client, `max_retries=0`, 12 runs each:

| model | `stream=False` | `stream=True` |
|---|---|---|
| `deepseek-v4-pro` | **8/12 = 67%** (avg 65s) | **0/12** (avg 78s) |
| `deepseek-v4-flash` | 0/12 (avg 33.5s) | 0/12 (avg 33.6s) |

Short requests never trigger it, which is why a trivial probe of `v4-pro`
passes; only sustained generation shows it. This is also exactly why the council
(which streams every call) kept working while the agent pipeline (plain
`invoke`) did not.

**There is no V3.2 option any more.** Verified against the live API on
2026-08-01: `/models` returns only `deepseek-v4-flash` and `deepseek-v4-pro`,
and the old names are thin aliases onto V4 — `deepseek-reasoner` →
`deepseek-v4-flash` with thinking on, `deepseek-chat` → `deepseek-v4-flash` with
thinking off. Any earlier note recommending "V3.2 for stability" was really
just selecting `v4-flash`.

That explains the fleet-level numbers: screening error rates (share of ticker
rows scored `-99`) were ~0% through 2026-07-23, then 72–100% per run from
2026-07-24, when `v4-pro` became the deep model. It also explains the observed
whole-run rate — deep-model calls are roughly one call in seven, and
`0.14 × 0.875 ≈ 12%` matches the 12.4% per-call drop rate measured across an
instrumented screen (40 drops / 323 calls).

**Why some tickers finish and others don't**: it is a per-call dice roll and a
depth-3 ticker makes ~80 calls in a row, any one of which kills it. Survival is
`(1 - q)^80` for unrecovered-drop rate `q` — 1.7% → 25% of tickers survive
(matches the 78% error rate observed with the old 2 retries), 0.31% → 78%
(matches 5-of-6 with `max_retries=5`). Nothing about the ticker matters.

Mitigations, in order of effectiveness:
1. **DeepSeek calls stream by default** (`llm_kwargs.setdefault("streaming",
   True)` in `openai_client.py`). This is the actual fix — it takes `v4-pro`
   from 67% drops to 0% and costs `v4-flash` nothing (33.5s vs 33.6s). Verified
   to preserve tool calls and `with_structured_output`. Set `streaming=False`
   in config to opt out.
2. `DEEPSEEK_MAX_RETRIES = 5` (up from the SDK's 2). Defence in depth for
   whatever still slips through; drops are probabilistic, so retrying the single
   failed call is far cheaper than losing a whole ticker's multi-agent run.
   Measured on 8 tickers with non-streamed `v4-pro`: 78% → 17% error rate.
3. Model choice is now a cost/quality decision rather than a reliability one:
   `v4-pro` is ~3x the token price of `v4-flash` ($0.435/$0.87 vs $0.14/$0.28
   per 1M in/out). Both are 1M context, 384K max output.

---

## 13. Dashboard views (`cli/static/reports_site.html`)

Single-page app, one section per view, routed by URL path when served by
`cli/server.py` (`/screenings`, `/analyses`, …) and by `#hash` when the built
static file is opened over `file://`. Adding a view means updating four places:
the `VIEWS` array, the sidebar `ni-<view>` nav item, the `v-<view>` section, and
the `render()` dispatch — plus the clean-path route list in `cli/server.py`.

**Screenings** — earnings calendar + one accordion per screening run. Inside a
calendar day card, the screenings listed against a ticker are limited to those
run in the `SCREENING_WINDOW_DAYS` (30) before that print: an Aug 3 print shows
only screenings done since Jul 3. Recency is the rule, deliberately *not* the
run's own `earnings_date` label — a batch is tagged with the date it targeted,
but a ticker inside it can report a few days later (WHR sits in the Jul 27 batch
yet prints Aug 3), and that screening is the relevant one. Runs dated after the
print are kept only when explicitly tagged for it, so a later cycle can't bleed
backwards. Without this, every screening ever run against a quarterly reporter
showed up on every day card, and the 25 runs carrying no `earnings_date` leaked
into all of them. The `×N` history badge and its modal use the same window, with
a **Show all** escape hatch that drops back to the unfiltered history.

**Analyses** — every ticker-level analysis in one browsable, searchable place:
the per-ticker work from each screening run plus standalone `analyze` runs
(`reports/analysis/`), which are otherwise only reachable by expanding the run
that produced them. Filter by ticker / source / signal; paginated in pages of
150 since the list runs to a few thousand rows. Standalone entries get their
`signal`/`confidence`/`total_score`/`one_liner` parsed server-side in
`_build_reports_data` so they sort and filter alongside screened ones; an
`analysis`-type report with no score JSON block simply shows "—".

**Watchlist** — open WATCH entries sorted TRIGGERED → ARMED → PENDING, with
Purchased / Expired / Dismissed folded away below. `Now` is a live quote (green
dot; a grey dot means it fell back to the daily close) and re-polls every 60s
while the view is open. Each row carries **✓ Purchased** and **✕ Delete**, both
POSTing to `/api/watchlist/action` and both undoable from the folded section
they move into — the buttons record what the user did about an entry, they do
not place or cancel anything. They are hidden in the static `build-web` output,
where there is no server to write the state file.

**Screenings — earnings calendar.** Day cards render **headers only**; the table
for a day is built the first time it is expanded (`calDayBodyHtml`, cached via
`data-built`). Rendering all five days eagerly produced ~683 KB of HTML and 546
checkboxes, every one inside a `display:none` container — the single slowest
thing on the page after the payload itself. A **market-cap filter** (min/max,
`Micro/Small/Mid/Large` presets, and an "include unknown cap" toggle) narrows
which companies appear; it persists in `localStorage`, the day header reports
"31 of 168 companies", and `calDayTickers()` makes "Screen Selected ▶" honour it
whether the day is open (checkbox selection) or collapsed (everything matching
the filter). Inputs take bare numbers as millions, or a K/M/B/T suffix.

**Run** — job launchers (screen / analyze / calibrate / reflect / improve /
allocate), plus a **Resume** card listing runs that are missing tickers or an
allocation. Each row shows `screened/total`, the missing count, and whether the
universe came from the earnings calendar or only from the run's own folders;
the button starts a `resume` job and streams its log over the same websocket as
every other job. A red sidebar badge on **Run** counts jobs the registry has
found dead (see §14).

**Performance — portfolio returns.** TWR and MWR both divide by a **capital
base**, not by Σ of every position's notional. The distinction is the account's
*turnover*, and getting it wrong is not a rounding error: $15.7M of notional
traded on a ~$840k book reported **+0.99%** for a period that returned ~**+18%**.

Entry dates are not in the trade log — IBKR's Flex closing record omits the open
date, so `trade_date` is null on every imported fill — which rules out a true IRR
or a time-integrated average exposure. What survives without them: *trades that
exit on the same day were provably open on that day*, so the largest same-day
exit notional is a hard lower bound on capital at work. `dashCapitalBase()`
returns it. Being a lower bound, it can understate the return but never inflate
it — the safe direction for a number you might act on.

* `MWR = Σ P&L ÷ capital base`
* `TWR = Π(1 + daily P&L ÷ capital base) − 1`, chain-linked over exit **days**.
  Chaining one HPR per *trade* instead treats concurrent positions as if each
  reinvested the previous one's proceeds; across 1,322 overlapping trades
  (median 15 exits/day) that compounded a +18% account into a reported
  **+1,580%** cumulative and **+1,515,750%** annualized.

Annualization is `(1+r)^(365/days) − 1`, guarded for `r ≤ −1` so a wiped-out
period renders `—` instead of `NaN`, and skipped under 7 days.

Both dashboards carry their own copy (`reports_site.html`, the legacy
`dashboard.html`), so `tests/test_portfolio_returns.py` runs the real functions
out of the HTML under node and asserts the two agree. If entry dates ever land
in the log, replace the bound with true average capital employed
(`Σ notional×days ÷ period`) — the call sites already take a single `base`.

---

## 14. Long-running jobs: registry, banner, resume

Dashboard jobs execute *inside* the uvicorn process (`_run_screen` and friends in
`cli/server.py`). That has one sharp edge: the server holds `cli/` and
`tradingagents/` in memory from process start, so picking up a code change means
a restart — and the restart kills every in-flight job. On 2026-08-10 that ended a
149-ticker screen at ticker 54, silently, with the UI still claiming nothing was
running.

Three pieces close that hole.

**`cli/jobs_registry.py` — durable job list.** Every job is mirrored to
`reports/jobs.json` (next to `trades.json`, so Syncthing carries it) with its
PID, run folder and ticker total. Two properties make it honest rather than
merely persistent:

* *Liveness is derived, not trusted.* A stored "running" is checked by signalling
  the PID at read time; a job whose process is gone reads back as `interrupted`.
* *Progress is recomputed, not reported.* For a run-folder job, "done" is a count
  of `earnings_brief.md` files on disk — so a job that never calls back, or one
  started by a script that predates the registry, still shows real progress.

Records are advisory: losing the file loses visibility, never work. Writes are
atomic (`os.replace`) and finished records are pruned after 7 days.

**`/api/jobs` — merged view.** Returns this process's in-memory jobs *plus*
registry records it has never heard of, so the activity banner reflects
everything running on the machine: a detached CLI run, or a job that outlived a
previous server. External records are flagged `external: true`; the banner's log
modal shows their run folder and log path instead of trying to open a websocket
that does not exist here.

**`cli/commands/resume.py` — finishing an interrupted run.** `plan_resume()`
works out what is left: the intended universe comes from the earnings-calendar
entry the run was launched for (tickers are screened in calendar order, so the
finished set is a prefix of it), falling back to "folders started but empty" for
runs with no calendar backing. `resume_run()` then screens exactly the missing
tickers *into the same folder* through the shared `screen_ticker()`, rebuilds
`screening_table.md` from every brief, and re-runs allocation. Folders that were
started but produced nothing are retried first, since they are the earliest gaps.
`tradingagents resume` and the `resume` job type both call it, so CLI and
endpoint cannot drift.

**Stopping a job (`POST /api/jobs/{id}/stop`).** The banner's **■ Stop** button
ends a job that has wedged or is no longer wanted. The implementation is shaped
entirely by one fact: *a dashboard job is a thread in the server process, so its
recorded PID is the server's own.* Signalling that PID kills the dashboard along
with the job. So the endpoint branches on where the work actually lives:

| Where | What Stop does |
|---|---|
| This process (the usual case) | Cooperative cancel — no signal is ever sent |
| Another live process (detached CLI run) | `SIGTERM`, or `SIGKILL` with `force=true` |
| A process that is already gone | Persists the terminal status so the record stops claiming to run |

Cooperative cancel means `jobs_registry.request_cancel()` records the intent
(in-memory set *and* the file, so a job started by one process can be stopped
from another), and workers call `is_cancelled()` between units of work:

* a queued ticker is **never started**;
* a ticker already inside `screen_ticker()` **finishes** — a Python thread cannot
  be interrupted, and the honest UI copy says so rather than implying a kill;
* the 11-call allocation council is **skipped** on a deliberately partial run,
  while `screening_table.md` is still written for whatever completed, so
  **Resume** can finish the run later.

Two statuses, not one: `cancelling` while the work unwinds, `cancelled` once it
has (also inferred when a cancelling job's process disappears). `cancelling`
still counts as in-flight and stays on the banner — hiding it would recreate the
invisible-run problem the banner exists to prevent. And `cancelled` is kept
distinct from `interrupted` because only `interrupted` means work was lost
*unintentionally*, which is what drives the offer to resume.

Rule of thumb: anything expected to run for more than a few minutes should be
started detached (`nohup … &`) or via the CLI rather than the Run tab, so a
server restart is free. The registry makes such runs visible either way — and a
detached run is the one kind Stop can genuinely force.


---

## 15. Dashboard payload budget

The dashboard page grew to **27 MB and ~10 s to load**. The cause was not the
number of runs but what each ticker row carried: `earnings_brief_md` (8.9 MB
across 3,072 rows) and `portfolio_decision_md` (10.8 MB) — 72% of the payload —
shipped for every ticker of every run, when only one report is ever open at a
time.

`_build_reports_data()` takes the knobs that fix this:

| Parameter | Server | `build-web` |
|---|---|---|
| `limit_runs` / `run_offset` | 12, paged | all |
| `include_bodies` | `False` | `True` |
| `limit_trades` | **not limited** — see below | all |
| `limit_reflections` | 40 | all |
| `refresh_watchlist` | `False` | `True` |

The static build keeps everything: that page has no server behind it to fetch
from later. The live dashboard fetches the rest on demand — `/api/report` for
one ticker's markdown (cached client-side onto the record), `/api/runs`,
`/api/trades?limit=&offset=`, `/api/reflections` for older pages, each behind a
**Load more** button. `refresh_watchlist=False` keeps the per-ticker live-quote
round-trip (2–7 s) off the critical path; the page's own `/api/watchlist` call
fills prices in a moment after first paint.

Result: **1.3 MB, 0.07 s warm** (1.3 s cold) — a 23× smaller payload.

Two invariants worth preserving:

* A trimmed payload must not cost *metadata*. Scores are parsed out of the brief
  server-side, so `include_bodies=False` still yields `total_score`, `signal`
  and the rest — only the prose is withheld, flagged by `has_brief` /
  `has_decision` so the UI can tell "not loaded" from "not there".
* Views derived from runs (Analyses) show "the N most recent runs of M" and
  offer the same paging, rather than quietly presenting a partial set as
  complete.

**Trades are the exception to the paging rule.** The Overview builds its stat
tiles, charts and equity curve *client-side* from `DATA.trades`, so a truncated
log does not look incomplete — it silently reports a smaller lifetime P&L
($28k instead of $143k when the first payload carried 300 of 1,262 fills). The
whole log is ~800 B a fill, under 1 MB, so it ships in full;
`/api/trades?limit=&offset=` remains for callers that want a page. Server-side
`stats` are computed from the untruncated list regardless, so the two can never
disagree again.

The general rule: **a limit is safe for a list the UI only ever displays, and
unsafe for one it aggregates.** Runs and reflections are displayed; trades are
aggregated.

**Handlers that do blocking work must be `def`, not `async def`.** Starlette runs
a sync handler in a threadpool and an async one on the event loop, so an `async
def` that blocks stalls every concurrent request: a cold `/api/watchlist` (~8 s
of quotes) made the Screenings page's 2 ms calendar request look like a five-
second one. None of these handlers await anything.

**When adding a field to the payload, price it × 3,000 rows.** Anything
report-sized belongs behind `/api/report`, not in the first paint.

### Post-paint repaints

A fast first paint is not enough: the page also felt stuttery because content
kept arriving a beat later. Two causes, both now closed.

**The watchlist could not be right at paint time.** TRIGGERED is the one status
that needs a live quote, so a payload built with `refresh_watchlist=False`
showed ARMED rows that flipped a second in — and the Overview's alert strip, the
most actionable thing on the page, always arrived late. Fixed at three levels:

* `cli/server.py` keeps one **stale-while-revalidate snapshot** (`_WL_SNAP`,
  `watchlist_snapshot()`, 45 s TTL). A request takes whatever is cached and
  kicks a background refresh; only the first caller after startup can block,
  and `/` passes `block_if_cold=False` so it never does. `@app.on_event
  ("startup")` warms it, and `POST /api/watchlist/action` republishes it,
  since the user overlay it just wrote makes the snapshot wrong by definition.
* `collect_watchlists()` fetches concurrently and only for rows that actually
  get a live refresh (not dismissed, not stale). The dominant cost was never
  the quotes — it was one sequential yfinance *history* call per entry (~150 ms
  each) for the trigger scan, now `fetch_bars()` behind a 300 s TTL cache.
  Both caches are process-global: `clear_caches()` exists because one test's
  bars once answered another test's deliberately-empty fetch.
* The frontend repaints only on a real change (`wlSig()` over what the UI
  draws), and the alert lives in a stable `#wl-alert-slot` updated by
  `wlPaintAlert()` — inserting it used to re-run `renderOverview()`, rebuilding
  every chart below it to add one strip.

Measured: `/api/watchlist` 2.3–6.7 s → **1 ms**; the confirming boot fetch is
now byte-identical to the embed, so it repaints nothing.

**`renderScreenings()` re-downloaded the world.** It fetched all of `/api/data`
(3.2 MB, 1.9 s) on *every* visit to the view, purely to notice runs that had
finished since load — a full re-download and a main-thread `JSON.parse` for a
payload almost always identical to the embedded one. Now `_scrCheck()` polls
`/api/screening-runs` (names + ticker counts + allocation flags, 11 KB, 40 ms)
and only calls `_scrPullRuns()` (`/api/runs`, first page, merged so loaded pages
survive) when that signature moves. The first probe just records the signature:
the page was served with that exact state.

The rule: **after the first paint, fetch to compare, not to replace.** A cheap
probe plus a change check beats re-fetching a payload you already have.


---

## 16. Diagnosing a wedged run

A batch screen that stops writing artifacts while the job still says "running"
has happened three times, each with a different cause. What settles it quickly:

1. **Is it actually stuck?** Compare the newest file in the run folder against
   the clock, and check process CPU. Idle + ~0% CPU is a block, not slow work.
2. **Dump the stacks — this is the step worth doing first.**
   `sudo .venv/bin/py-spy dump --pid <server pid>` (root is required on macOS,
   as is `sample`). One dump names the exact blocking line in every thread.
3. Only then reason about sockets. `lsof -nP -p <pid>` shows where *connections*
   are, which is not where *threads* are: a 2026-08-13 hang showed 73 Yahoo
   sockets against 1 DeepSeek and looked like a yfinance problem, while all 8
   workers were in fact blocked reading a DeepSeek stream. The Yahoo sockets
   were idle keep-alives.

Timeout policy for DeepSeek lives in `llm_clients/openai_client.py` and splits
three clocks deliberately — short **connect** (a dead network should surface in
seconds), **read** that differs by mode (on a stream it bounds the gap between
chunks, off one it must cover the whole silent reasoning phase), and generous
write/pool. `DEEPSEEK_MAX_RETRIES` multiplies whichever read timeout applies, so
raising either compounds.

Recovery is always the same: the completed tickers are on disk, so restart and
`tradingagents resume --dir <run>` — detached, not through the dashboard.
