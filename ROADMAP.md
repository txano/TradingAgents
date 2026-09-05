# TradingAgents — Roadmap

Personal pre-earnings trading system built on top of TradingAgents.
This file tracks the planned improvements in priority order.

---

## Status legend
- `[ ]` Not started
- `[~]` In progress
- `[x]` Done

---

## Recommended build sequence (priority order — overrides section numbers)

After the v2 playbook review (2026-06-14), section numbers no longer reflect
priority. Build in this order — free, high-leverage, loss-preventing work first.
Items marked ⟐ are now **corroborated by the `learn` loop on real trade history**
(2026-06-16) — the system's own post-mortems keep asking for them.

1. **#14 — Expectations-Gap & Payoff-Asymmetry engine** — **DONE** (14a asymmetry engine, 14b EV hard gate + soft fade/coverage downgrade, 14c run-up/crowding gate). Open follow-ups only: better `P(beat)` via #1 calibration, and premium-selling routing via #5. ⟐ The `learn` loop already de-weighted `beat` (0.7→0.55) and rewrote the earnings prompt around beat-quality / run-up / "priced-in" — exactly this thesis.
2. **#9 — Peer earnings read-through** — **DONE** (2026-06-23, `earnings/peers.py`): curated peer map → EPS surprise + day-1 reaction for peers that reported in the last ~35d, feeding the brief prompt, a `Peers:` council line, and the beat-and-still-fall downgrade. Also shipped the **#17 log-schema-v2 prerequisite** (`trade_log.py` + `backfill-trades`).
3. **#15 — Implied-move sizing cap + tactical regime gates** — **DONE** (2026-07-18, `allocation/regime.py` + validator/council wiring): hard implied-move loss cap (1% of budget), risk-off gate (SPX<50dma or VIX>22 → cap halves), FOMC/CPI collision gate (±2 sessions → halves again). Also shipped Sharpe/Sortino/MaxDD in `stats` (`trade_log.risk_stats`) and the `regime_flag` trade-log backfill.
4. **#19 — Conditional "wait & decide" entries** — **DONE** (2026-07-18, `allocation/watchlist.py`): WATCH council outcome + trigger level, validator gates, `watchlist.json` lifecycle (PENDING→ARMED→TRIGGERED→EXPIRED), dedicated dashboard view with nav badge + Overview alert strip, `wait_and_decide` trade tagging.
5. **#10 — Post-earnings dislocation scanner** (free; a second, independent trade source; #19 covers already-screened names — this scans the rest) ← NEXT
6. **#1 — Calibration extensions + `suggest-weights`** (turns the above into measured, self-tuning gates)
7. **#16 — Trade management: exit map + post-earnings triage** ⟐ (high P&L leverage; the single most-repeated `learn` process note — wins closed too early; needs live position tracking)
8. **#17 — Backtest harness** (validates every gate/threshold on your own log; gated on trade volume)
9. **#20 — Pair trading / peer-hedged entries** (PEER_MAP-based hedge leg; needs a divergence heuristic — after #16, benefits from #15's regime gates)
10. **#6b — Shiller CAPE / Buffett indicator** (cheap structural regime context; stacks with the #15 tactical gates)
11. **#11 — Multi-model ensemble** · **#3b — IBKR options pipeline** (IV rank / term structure / skew, once free signals are mined)
12. Everything else (#4 sector memory, #5 options structures, #6a macro scraping, #7 social, #8 Hermes, #12 OSS analysts, #13 dream-mode)

> Source material for #14–#17 lives in `docs/research/earnings_playbook_v2.md`
> (decision framework) and `docs/research/earnings_toolkit_ibkr_reference.py`
> (a working `ib_async` options-snapshot script — the reference implementation for #3b).
> **#18** captures on-strategy ideas mined from the saved investing-reels feed
> (2026-06-23); its biggest contribution — a strategy-validation methodology — is
> folded into **#17** and is the cure for #14/#15's unvalidated-threshold caveat.

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
- [ ] Extend `correlation` to the new buckets: `fundamentals_score` and options-implied move vs. actual outcomes (both are now stored per ticker in `fundamentals_score.json` / `pricing.json`)
- [ ] `suggest-weights` command: compute per-bucket signal accuracy from calibration + trades data and propose new allocation weights (user confirms before writing) — see ARCHITECTURE.md §10
- [ ] **Guide-dominance test** (playbook Part 5 #4): split realized P&L by `guide` outcome regardless of beat. If guidance explains more day-1 P&L variance than the beat, that is empirical justification to raise `guidance_weight` over `beat_weight` — feeds directly into `suggest-weights`

### Notes
- `reflect` command + `trades.json` already capture trade outcomes; this builds the prediction side on top
- Priority: run after a few weeks of data accumulate to have a meaningful sample
- The council prompt no longer claims weights are calibration-derived; once `suggest-weights` exists and is used, that claim becomes true

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
> Partially superseded: the **options-implied earnings move** (ATM straddle for the
> first expiry after earnings) is now computed from yfinance options chains in
> `tradingagents/allocation/pricing.py`, saved to each ticker's `pricing.json`, and
> fed to the allocation council with a sizing rule. The IBKR items below remain
> relevant for richer data (IV rank, flow, order book) and for feeding the
> earnings-brief `setup_score` rather than only the council.
>
> **Reference implementation ready:** `docs/research/earnings_toolkit_ibkr_reference.py`
> already pulls implied move, ATM IV, term-structure ratio, and 25Δ skew via
> `ib_async` (the maintained successor to `ib_insync`). Per the v2 review this is
> deferred until the free signals (#14/#15) ship, then folded in here.
> Decision (2026-06-14): keep in backlog, free signals first.
- [ ] Set up `ib_async` connection to IBKR TWS/Gateway (toolkit is the starting point)
- [ ] Fetch **Implied Volatility** for front-month options (most important: tells you what the market is pricing in for the earnings move)
- [ ] Compute **Expected Move** = stock price × IV × √(days_to_earnings / 365)
- [ ] Fetch **IV Rank / IV Percentile** (current IV vs. 52-week range) — high IVR = expensive options, harder to profit directionally; **log IV daily now** so a real 52-week IV-rank history self-builds (IBKR won't hand you history cheaply)
- [ ] **Event-premium / term structure** = front-expiry ATM IV ÷ next-monthly ATM IV (> 1.4 = heavy event speculation, violent IV crush likely)
- [ ] **25Δ skew** = 25Δ put IV − 25Δ call IV (put skew = hedged longs; rich call skew = squeeze fuel + fade risk)
- [ ] Fetch **Put/Call Open Interest Ratio** at nearest strikes — skew indicator
- [ ] Feed these into the earnings brief prompt as a new `IBKR DATA` section
- [ ] Update `setup_score` scoring guide to incorporate IV data (e.g., heavily penalise setups where expected move already exceeds our predicted move)

### 3c — Options volume & additional IBKR market data
- [ ] Fetch **options volume** (calls vs. puts) from IBKR — unusual options activity signals smart-money positioning
- [ ] Fetch **historical options flow** — track daily volume spikes in the days leading up to earnings
- [ ] Expose **level 2 order book** depth for large-cap names if available via IBKR feed
- [ ] Pull **short interest** and **days-to-cover** from IBKR where available
- [ ] Add a dedicated `IBKR FLOW DATA` section to the earnings brief prompt

### Notes
- Items in 3b/3c require IBKR TWS/Gateway running locally
- IV + Expected Move are the highest-priority additions for this specific use case — without them, a "BUY" call ignores whether the move is already priced in
- Library: `ib_insync` (pip install ib_insync) — simpler than native ibapi

---

## #4 — Sector Tracking & Memory

**Goal:** Build sector-level pattern recognition on top of per-stock analysis.

### Phase A — Sector column (quick win)
- [x] Add `sector` field to screening results (fetch from `yfinance.Ticker.info["sector"]`)
- [x] Show sector column in the screening table and allocation report
- [x] Track sector concentration in the allocation manager — enforced deterministically by `allocation/validator.py` (35%-of-budget sector cap, violations re-prompted then flagged in the report)

### Phase B — Sector memory
- [ ] Create `memory/sectors/` folder structure — one file per sector (Technology, Healthcare, etc.)
- [ ] After each screening + calibration cycle, append: what we predicted, what happened, pattern notes
- [ ] Feed relevant sector memory into the earnings brief prompt as additional context
- [ ] Track sector-level accuracy separately in `tradingagents stats`

### Phase C — Sector news feed + sector-specific briefings (longer term)
- [ ] Daily/weekly sector news ingestion (yfinance or news API)
- [ ] LLM summarisation per sector, stored in sector memory files
- [ ] Surface in earnings briefs for companies in that sector
- [ ] **Sector-specific briefings:** before running screening on a set of tickers, generate a 1-page macro briefing per target sector (e.g. "Semiconductors — week of 2026-05-12") that aggregates earnings season trends, analyst sentiment, and macro tailwinds/headwinds for that sector
- [ ] Allow user to provide manual sector notes/theses that get injected into the briefing
- [ ] Feed sector briefing into the earnings brief and allocation council prompts as additional context

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

## #6 — Macro Finance News Inflow

**Goal:** Keep the system aware of the broader macro environment by continuously ingesting finance newsletters, blogs, and curated pages so analysis is grounded in current macro context.

### Sub-tasks
- [ ] Define a curated source list (newsletters, substack feeds, macro blogs, central bank publications)
- [ ] Build a scraper/RSS ingestion pipeline that pulls new content on a schedule
- [ ] LLM summarisation layer: produce a daily "macro brief" (rate environment, inflation signals, credit spreads, sector rotation trends)
- [ ] Store macro briefs in `~/.tradingagents/macro/YYYY-MM-DD.md`
- [ ] Inject the most recent macro brief into the allocation council and earnings brief prompts
- [ ] Surface macro brief in the dashboard

### 6b — Quantitative market regime gauges (quick win, independent of 6a scraping)
- [ ] Fetch **Shiller CAPE** (multpl.com or Robert Shiller's published dataset)
- [ ] Compute the **Buffett indicator** (total US market cap / GDP, via FRED: Wilshire 5000 proxy + GDP)
- [ ] Classify regime (cheap / fair / stretched / extreme vs. historical percentiles) and store with each screening run
- [ ] Inject the regime line into the council synthesis prompt as an **exposure governor**: stretched regime → favour more cash reserved and smaller sizing tiers, never as a per-ticker signal

### Notes
- Prioritise sources that publish on a clear schedule (weekly newsletters, Fed minutes) before real-time scraping
- Macro context matters most for allocation sizing (risk-off environment → smaller positions) — that is the first integration point
- 6b is much cheaper than 6a (two numbers, monthly cadence) and can ship first

---

## #7 — Social Media Scrapers for Signal Enrichment

**Goal:** Tap into retail and institutional sentiment signals from X, Reddit, Seeking Alpha, and StubHub to enrich the social/news analysts.

### Sub-tasks
- [ ] **X (Twitter):** scrape cashtag feeds ($TICKER) around earnings — volume of mentions, sentiment ratio, notable accounts
- [ ] **Reddit:** scrape r/wallstreetbets, r/investing, r/stocks for ticker mentions and sentiment in the week before earnings
- [ ] **Seeking Alpha:** scrape article headlines + comment sentiment for the ticker
- [ ] **StubHub / event data:** parse event-level demand signals for consumer/entertainment names (e.g. ticket sell-through for a media company's IP)
- [ ] Normalise all sources into a shared `SocialSignal` schema: source, date, sentiment_score, volume_score, notable_mentions
- [ ] Feed aggregated signals into the social analyst prompt and store in the report folder

### Notes
- Legal/ToS: X requires API v2 (rate-limited); Reddit has PRAW; Seeking Alpha has no public API — scraping carries ToS risk, use responsibly
- StubHub signal is highly niche but powerful for consumer/entertainment earnings (e.g. LIVE, DIS, LYV) — flag in sector briefing when applicable
- Start with Reddit (PRAW, free) and Seeking Alpha headlines before tackling X paid API

---

## #8 — Hermes Self-Improving Agent Integration

**Goal:** Integrate [Hermes](https://hermes-agent.nousresearch.com/) (NousResearch self-improving agent) as an enhancement to the allocation and memory layers, allowing the system to learn from its own decisions over time.

### Sub-tasks
- [ ] **Available today, zero code:** add a Hermes model as a council advisor via `config["council_advisor_models"]` (e.g. `"openrouter:nousresearch/hermes-..."` or `"ollama:hermes3"`) — the round-robin multi-model advisor plumbing already exists in `council.py`
- [ ] Evaluate Hermes API / self-hosted deployment options
- [ ] Wire Hermes into the **allocation council** as an additional advisor with access to the full trade history and calibration data
- [ ] Give Hermes access to `trades.json`, `calibration.json`, and sector memory files so it can surface patterns the static LLM prompts miss
- [ ] Implement a feedback loop: after each earnings cycle, Hermes reviews its allocation recommendation vs. actual outcome and updates its internal heuristics
- [ ] Evaluate whether Hermes can replace or augment the current multi-round council (council.py) for the synthesis step

### Notes
- Self-improvement capability is particularly valuable for the allocation layer where historical pattern recognition compounds over time
- Dependency: needs a meaningful trade history (#2) and calibration data (#1) before the self-improvement loop has signal to learn from
- Revisit after ~6 months of live trades and 20+ calibration cycles

---

## #9 — Peer Earnings Read-Through (HIGH priority) ✅ DONE (2026-06-23) — `tradingagents/earnings/peers.py`

**Goal:** When a ticker is about to report, exploit what its industry peers that *already reported this season* revealed — the single most predictive free signal the system currently ignores.

### Sub-tasks
- [x] Identify peers per ticker: curated `PEER_MAP` keyed on the names we trade most, clustered by industry (semis, solar, SMR/nuclear, autos, restaurants, footwear, LatAm fintech, miners, steel, …). Unmapped tickers yield an honest "no peer map" rather than a guess (yfinance exposes no peer list)
- [x] For peers that reported in the last ~35 days, fetch EPS surprise + post-earnings day-1 price move — reuses `allocation/asymmetry.fetch_earnings_reactions` (one `earnings_dates` + price-history call per peer)
- [x] Add a `PEER READ-THROUGH` section to the earnings brief prompt (peer, days ago, beat/miss, surprise, day-1 reaction, one-line takeaway)
- [x] Update the `guidance_score` / `setup_score` scoring guides to reference peer results (peers beating + guiding up → tailwind; peers missing on the same driver → penalise)
- [x] **Beat-and-still-fall signal** (playbook Block B gate 5): when same-sector peers *beat and still closed red*, `sector_bar_elevated` flips True; the brief prompt and the council synthesis prompt both downgrade conviction one notch. Distinct from a miss — the most dangerous pattern (good results, bad reaction)
- [x] Surface a one-line peer summary on the council `Peers:` line (`format_peer_oneliner`); persisted per ticker as `peers.json`, live fallback in `allocation/layer.py`

### Notes
- Strongest in clustered industries (semis, banks, airlines, retail) where read-through is well documented
- No new data source needed — reuses the yfinance plumbing from `allocation/asymmetry.py` (same fetch the #14 engine already uses)
- **Follow-ups:** widen `PEER_MAP` coverage as new names are traded; add revenue-surprise + an explicit guidance-reaction read (currently EPS surprise + price reaction only); consider SEC/industry peer auto-discovery to reduce the curated map's maintenance

---

## #10 — Post-Earnings Dislocation Scanner (HIGH priority)

**Goal:** A second source of trades: tickers that *dropped after reporting* while their fundamentals stayed intact — buy-the-dip candidates where the market punished the print but the business didn't change.

### Sub-tasks
- [ ] New `tradingagents dislocations` command: scan past screening folders for tickers that have reported since
- [ ] For each, compare current price vs. pre-earnings price, and re-fetch fundamentals metrics (`fetch_fundamental_metrics`) to re-score quality
- [ ] Flag candidates where price is down ≥ X% (configurable, default ~8%) AND `fundamentals_score` ≥ +2 AND not materially below the stored pre-earnings score
- [ ] Output a ranked dislocation table; optionally run a slim council pass (advisors + synthesis, no cross-review) to size entries
- [ ] Log resulting trades in `trades.json` with a distinct `strategy: "dislocation"` tag so calibration tracks this strategy separately from pre-earnings plays

### Notes
- Cheap to build: `fundamentals_score.json` and `pricing.json` are already saved per ticker per run — the scanner mostly reads existing artifacts plus one price fetch
- Keep it separate in stats: this is mean-reversion, a different bet than the pre-earnings event trade

---

## #11 — Multi-Model Ensemble for Scoring & PM Decision (MEDIUM priority)

**Goal:** Run the same prompt on 2–3 different models and use agreement as a confidence signal — divergence between models is information, not noise.

### Sub-tasks
- [ ] Config key `ensemble_models` (list of `"provider:model"`, same format as `council_advisor_models`) + an `--ensemble` opt-in flag on `screen`/`analyze` (cost multiplier, so off by default)
- [ ] **Earnings scoring:** run the brief scorer on each model; per bucket take the median score and record dispersion; add a dispersion column to `screening_table.md` (high dispersion → uncertainty flag)
- [ ] **Portfolio Manager decision:** run BUY/SHORT/SKIP on each model; majority vote wins, any split automatically downgrades conviction one tier
- [ ] Persist per-model outputs in the ticker folder so calibration can later answer "which model is actually most accurate on this strategy?"

### Notes
- Reuses the `build_advisor_llms` / provider:model plumbing added for the council — mostly orchestration work
- Start with scoring + PM only (as decided); council-synthesis diffing can come later if dispersion proves informative

---

## #12 — External OSS Analyst Signals (MEDIUM-LOW priority)

**Goal:** Wire open-source trading analysts (ai-hedge-fund, Kronos, dexter) into the council's ticker context as additional live signals.

### Sub-tasks
- [ ] Define an `ExternalAnalyst` adapter interface: `name`, `analyze(ticker, date) -> {signal, confidence, rationale}`
- [ ] Adapter: **ai-hedge-fund** (virattt) — multi-agent investor personas; map its final verdict into the schema
- [ ] Adapter: **Kronos** — K-line foundation model price forecast; map predicted move direction/magnitude
- [ ] Adapter: **dexter** — evaluate what it actually provides and whether it adds non-redundant signal before committing
- [ ] Add an `EXTERNAL ANALYSTS` section to the council ticker context (clearly labelled as outside opinions)
- [ ] Track per-source accuracy in calibration from day one so weak sources can be pruned quickly

### Notes
- Decision: integrate live rather than benchmark offline first — accepted trade-off is higher build/maintenance cost before predictive value is proven, so the per-source accuracy tracking is non-negotiable
- Pin versions of each external repo; they move fast and break

---

## #13 — Idle-Time Self-Improvement Loop ("dream mode") (core BUILT; scheduling pending)

**Goal:** When the system is idle, it works on itself: re-checks predictions, refreshes lessons, and improves its own prompts/weights — inspired by Claude's background self-improvement behavior.

### Sub-tasks
- [x] **`learn` command (the core loop):** reflect on all trades → analyse all reflections in one deep-think call → auto-apply scoring-weight changes and prompt-source edits. Output (report, `proposal.json`, `CHANGELOG.md`, `backups/`) lands in `reports/self_improve_TIMESTAMP/`. Implemented in `tradingagents/learning/trade_reflections.py` + `self_improve.py`
- [x] **Safety harness for auto prompt edits:** allowlisted agent files only, exact unique anchor match, f-string `{placeholder}` preservation, per-file backup, post-write `compile()` check with auto-restore on syntax break. `--dry-run` / `--no-weights` / `--no-prompt-edits` for control
- [ ] Schedule `learn` nightly (cron or a Claude Code scheduled agent), gated to run only when new reflections exist
- [ ] Fold in `calibrate` (for screening runs whose earnings have passed) → `correlation` → `suggest-weights` (#1) so weight changes are calibration-driven, not only reflection-LLM opinion
- [ ] Surface the latest self-improvement report + changelog in the dashboard
- [ ] Stretch: widen the editable-prompt allowlist coverage and add a per-run A/B note so prompt drift can be measured against outcomes

### Notes
- **Design decision (2026-06-14):** the loop **auto-applies** (incl. prompt edits) rather than propose-only — made safe by the harness above plus git visibility (every change is `git diff`/`git checkout`-revertible and backed up per run). Use `--dry-run` to preview
- Complements #8 (Hermes): this is the scheduling/feedback skeleton; Hermes could later be the engine that reasons inside it
- Most useful once trade volume accumulates; the reflect-all half is valuable immediately for cleaning up the existing trade history

---

## #14 — Expectations-Gap & Payoff-Asymmetry Engine (TOP priority, free)

**Goal:** Reframe the system's objective. Today it answers *"will they beat and raise?"*; the money is in *"is my expected outcome better than what's already priced in, and is the payoff asymmetric in my favor?"* This is the single highest-value idea from the v2 playbook (`docs/research/earnings_playbook_v2.md`, Parts 1 & decision rules) and it is **free** — it runs on historical prints we can already fetch via yfinance.

### 14a — Payoff asymmetry from historical prints (Block C) ✅ DONE (2026-06-16)
- [x] For each ticker, from its last 8 quarterly prints compute `E[move | beat]`, `E[move | miss]`, **fade rate**, and **coverage ratio** (avg |actual move| ÷ implied move) — `tradingagents/allocation/asymmetry.py`
- [x] Compute **EV = P(beat) × E[move|beat] + (1 − P(beat)) × E[move|miss]**; `P(beat)` from a documented `beat_score_to_p_beat` heuristic (−5→0.25 … +5→0.75, clamped), replaceable by calibration in #1
- [x] Persist per ticker as `asymmetry.json` (written at screen time, read into the allocation context; live fallback in `layer.py`); shown to the council as an advisory `Asymmetry/EV:` line + interpretation rule in the synthesis prompt
- **Finding:** quality names beat ~every quarter → **0 misses in 8 prints**, so `E[move|miss]` and therefore `EV` are often `None` (AAPL/NVDA both 8B/0M on live test). But **fade rate is independently decisive** — AAPL shows **62% of beats close red, coverage 0.63x** (moves *less* than priced in): a textbook "don't go long into the print" signal that needs no miss sample. EV's missing-miss case is a **#14b** decision (proxy `E[miss]` from implied move, or lean on fade/coverage when EV is null).

### 14b — The expectations-gap decision rule (Block A) ✅ MOSTLY DONE (2026-06-22)
- [x] Advisory feed: EV, E[move|beat/miss], fade rate, coverage ratio in the council ticker context + a synthesis rule reading them
- [x] **Hard gate** (`validate_allocation`): a long is a violation when EV is computable and `EV ≤ 0` or `EV/implied_move < 0.25` → triggers the corrective re-prompt, then the Constraint Check section. Only fires for BUYs; SHORTs and no-asymmetry tickers are exempt
- [x] **No-miss fallback = soft downgrade** (per 2026-06-21 decision): when EV is null (quality names with no recent misses) but `fade_rate ≥ 0.60` or `coverage ≤ 0.70`, `asymmetry_advisories()` emits a non-blocking "size one tier smaller" note in a separate **Asymmetry Advisories** section; the synthesis prompt also tells the LLM to downgrade one tier. Thresholds are tunable module constants in `validator.py` for #17 to calibrate
- [ ] **Still open:** estimate our expected post-print move conditioned on the beat/raise prediction (vs. the flat `beat_score→p_beat` heuristic used now) — better `P(beat)` belongs with #1 calibration
- [ ] **Still open:** where asymmetry is bad but the name is attractive, route to a premium-selling structure instead of owning stock (links to #5)

### 14c — Crowding / run-up gate (Block B) ✅ DONE (2026-06-22) — `tradingagents/allocation/crowding.py`
- [x] **Run-up:** 1m and 3m return, absolute and vs. the sector ETF (SECTOR_ETF map → XLK/XLV/XLE/…, SPY fallback; per-day ETF cache). Flags at `> +12%` absolute or `> +8%` vs. sector
- [x] **Distance from 52-week high:** computed from `fast_info` (true `(high − price)/high`); flags within `3%` of the high
- [x] **Revision momentum (whisper proxy):** net up-revisions over 30d via yfinance `eps_revisions`; flags at `≥ 3` net up
- [x] All three are **soft** (per #14b decision): persisted to `crowding.json`, shown on the council `Crowding:` line + synthesis rule, and surfaced as a `crowding_advisories()` "size one tier smaller" note in the combined **Sizing Advisories** section. Thresholds are tunable constants in `crowding.py` for #17
- **Calibration note:** the `≥3 up-revisions` flag fires easily on heavily-covered mega-caps (NVDA showed 34/30d) — consider a coverage-relative threshold or higher cutoff when #17 calibrates

### Notes
- All free: historical prints, prices, and revisions are in yfinance; implied move is already in `pricing.json`
- This directly attacks the documented loss pattern — "even a 70%-accurate beat model has negative expectancy" on high-multiple names with −12%/+4% asymmetry
- Thresholds (0.6×, 0.25, +12%) are the playbook's priors — calibrate them on our own log via #17 once data exists

---

## #15 — Implied-Move Position Sizing & Tactical Regime Gates ✅ DONE (2026-07-18) — `tradingagents/allocation/regime.py`

**Goal:** Let the implied move (not conviction alone) set position size, and cut sizing in hostile regimes. Deterministic loss control that drops into `allocation/validator.py` and the council sizing rules.

> **Corroborated by `learn` (2026-06-16):** the self-improvement loop independently surfaced a **macro risk-off overlay** as a recurring process note across two runs (reduce size on yield spikes / geopolitical / oil shocks — cited losses: DLO, BKNG, GM, AG, REZI, OXY). It also recommended volatility-adjusted sizing. These can't be auto-applied as prompt edits, confirming the regime-gate work below is real and evidence-backed, not speculative.

### 15a — Implied-move sizing cap ✅
- [x] Hard cap: a position's *implied-move loss* (`amount × implied_move`) may not exceed `IMPLIED_MOVE_LOSS_CAP` = **1.0% of budget** (playbook suggests 0.75–1.0%); tunable constant in `validator.py` for #17 to calibrate
- [x] Effect: a ±10% implied-move name gets half the dollars of a ±5% name — enforced in `validate_allocation` (violation → corrective re-prompt → `⚠ Constraint Check`), applies to BUYs **and** SHORTs; the raw `implied_move_pct` is now on every ticker context. No implied move → no check (the prompt tells the LLM to size conservatively)
- [x] Stated in the synthesis prompt with the live regime-scaled dollar cap

### 15b — Tactical regime gates ✅ (distinct from #6b structural CAPE/Buffett)
- [x] **Risk-off gate:** SPX below its 50-dma or VIX > 22 → the loss cap halves (×0.5). Regime fetched once per allocation run (`fetch_regime`, yfinance ^GSPC/^VIX, truncatable to trade_date for reproducible re-runs), persisted as **`regime.json` at the run root**, shown as a `=== MARKET REGIME ===` block in the synthesis prompt
- [x] **Macro-collision gate:** FOMC/CPI within ±2 sessions of the print (static 2025–26 calendars in `regime.py`, business-day distance) → that ticker's cap halves again; per-ticker `Macro:` line in the council ticker sections ("⚠ COLLISION … halve or skip")
- [x] Multiplier logic in `regime.sizing_multiplier` — single source used by both the prompt and the validator, so the gate is enforced, not merely suggested
- [x] `backfill-trades` now fills `regime_flag` ("risk_off"/"normal") from the run's `regime.json` (#17 schema-v2 field, previously null-only)
- **Live validation (2026-07-18):** first real fetch classified the tape **risk-off** (SPX 7,458 marginally below its 50-dma 7,465, VIX 18.8) — the gate binds immediately.

### Notes
- VIX/SPX/50-dma and the econ-calendar check are cheap (yfinance + static FOMC/CPI date lists); no IBKR needed
- **Maintenance:** the FOMC/CPI calendars in `regime.py` are static — extend them each year (Fed publishes dates years ahead; BLS each autumn)
- #6b (Shiller/Buffett) is the slow structural governor; this is the fast tactical one — they stack
- Thresholds (VIX 22, 1% cap, ±2 sessions, ×0.5 multipliers) are playbook priors — tunable constants for #17 to calibrate

---

## #16 — Trade Management: Exit Map & Post-Earnings Triage (NEW capability)

**Goal:** The system is 100% entry-focused today. Parts 2 & 3 of the playbook formalize the exit side — where a lot of P&L is won or lost on a levered book. Decide the mapping *before* the print so post-print decisions take 60 seconds of classification, not an evening of agonizing.

> **Corroborated by `learn` (2026-06-16):** the self-improvement loop flagged **post-earnings exit rules** as a top recurring process note across both runs — trailing stops after gap-ups, scale-out on the open, volatility-adjusted stops + partial profit-taking when beat >15% or guidance is raised. Evidence cited that wins left money on the table by closing too early: EXEL, NUE, STX. This is the single most-repeated improvement the loop *can't* implement itself (it's a feature, not a prompt tweak), which makes it the strongest data-backed case for building this section.

### 16a — Outcome → Action map (Part 2)
- [ ] Encode the 7-row outcome→action table (beat+raise holds vs. fades, beat+soft-guide = exit, miss = exit-never-average, etc.) as a decision helper
- [ ] Inputs: outcome decomposition (beat EPS / beat rev / guide) + day-1 price action; output: action + horizon
- [ ] Bake in the drift nuance: day-1 direction is information for mid-caps/high-growth, but in mega-cap liquid names day 1 is ~the whole game

### 16b — Post-earnings triage tree (Part 3 — "I'm down: sell / hold / double")
- [ ] Gate 1 — thesis audit (miss / guide cut / narrative break → SELL, stop)
- [ ] Gate 2 — mechanical-drop diagnosis (ran >10% in, actual ≤ implied move, beat the whisper, peers fell on good prints, unwind volume profile — 3+ → mechanical)
- [ ] Gate 3 — let price confirm over days 2–5 (reclaim / base+time-stop / new-lows→sell)
- [ ] Gate 4 — the add: max 50% of original, hard stop on combined position at the post-earnings low, gross-exposure check first, one add per name per quarter
- [ ] Surface the gate path taken in the trade log (`gate_path` column)

### Dependencies & notes
- Requires **live position tracking** (current holdings + live prices) — we have `trades.json` for closed trades but no open-position state; that's the prerequisite build
- SELL is the answer at 3 of 4 gates; HOLD needs 2; ADD needs all 4 + a leverage check — the asymmetry is deliberate
- Highest leverage once live, but correctly sequenced after the free entry-side gates (#14/#15) and some trade history

---

## #17 — Backtest Harness (revived; validates gates on our own log)

**Goal:** Stop guessing thresholds. The v2 playbook (Part 5) gives five concrete backtests that turn the framework from "plausible" into "proven on my book" — or falsify parts of it, which is equally valuable. Previously deferred; revived 2026-06-14 because the recipes are now concrete and several gate thresholds (#14) depend on empirical calibration.

### Backtest recipes (Part 5, in priority order)
- [x] **Run-up test** (2026-08-30) — done on 1,128 screened prints (Jul–Aug 2026) rather than the trade log, which is too small. The prediction holds, and the **1-week** window is much sharper than the 1-month one: `corr(runup, day+10)` = −0.157 (1w) vs −0.110 (2w). Buckets, day+10: fell >5% into the print **+6.08%**; up 1–5% +0.57%; rose >5% +0.69%. Sector flow points the same way (sector ETF week, corr −0.121) but is largely redundant with the name's own run-up — it only discriminates once the name itself has run (name up >2%: +1.31% when the sector lagged, +0.07% when it led). Shipped as `runup_1w_pct` / `runup_1w_score` / `sector_1w_pct` / `sector_vs_spy_1w` in `crowding.py`, **display-only** so far.
- [ ] **Decide whether the 1-week run-up joins `weighted_score`.** It out-predicts every council score (beat +0.009, guidance −0.004, setup −0.053, total −0.016 against day+10), so this is the highest-value open scoring question. Blocked on a second earnings season — the bands in `RUNUP_1W_BANDS` come from one, in a rising tape, and the effect is plausibly short-horizon mean reversion rather than a durable edge.
- [ ] **Re-check score→outcome in `calibrate`.** Same sample: the numeric scores do not rank outcomes at all, and within BUY signals the top total-score bucket (9+) was the *worst* performer (−1.09% day-1). The BUY/SHORT signal itself does carry information (BUY +1.12% vs SHORT −1.43% day-1); the conviction number does not.
- [ ] **Implied-move test:** how often did wins exceed the typical implied move? If rarely, our edge has been direction-only and the EV filter (#14) adds the most value
- [ ] **Averaging-down counterfactual:** for every losing trade, simulate the #16 triage tree vs. what actually happened vs. naive double-down. Proves or falsifies the management framework on real price paths
- [ ] **Guide-dominance test:** P&L split by `guide` regardless of beat → if guide explains more variance, re-weight guidance over beat (feeds `suggest-weights`, #1)
- [ ] **Per-ticker fade rate:** for repeat names, compute Block-C stats; some names reveal as "never own into the print, always wait for the dip"

### Validation methodology (from "4 Steps to Validate a Quant Strategy" reel, 2026-06-23)
Apply this rigour to every tunable threshold in #14/#15 (EV/implied 0.25, fade 0.60, coverage 0.70, run-up 12%, sizing caps), not just to whole strategies:
- [ ] **Parameter-stability test:** sweep each threshold over a range, plot a heatmap of outcome vs. parameter. Robust = a broad plateau of good results; overfit = an isolated spike. This is the direct cure for "the thresholds are unvalidated priors"
- [ ] **Monte Carlo on every parameter set** (not one): bootstrap/reshuffle the trade sequence per setting to get an *outcome distribution* (and drawdown distribution), not a single backtest number
- [ ] **Cluster analysis** of the simulation results as a meta-check (do good settings cluster, or are they scattered noise?)
- [ ] **In-sample / out-of-sample + walk-forward** validation before trusting any threshold live
- [~] **Risk-adjusted metrics** in `stats`: ✅ Sharpe, Sortino, MaxDD shipped 2026-07-18 (`trade_log.risk_stats` — per-trade returns, equal-weighted, Sortino flags "∞" when no losers instead of dividing by zero); hit-rate already shown as win rate; turnover still open
- [ ] Engine references when we outgrow a hand-rolled harness: **VectorBT** / **Zipline** (Python), **Nautilus** (Rust, multi-venue), **ZipLime** (AI idea→code→backtest)

### Log schema v2 (prerequisite — Part 5) ✅ DONE (2026-06-23) — `tradingagents/trade_log.py`
- [x] Extend the trade log with T-1 context (`implied_move_pct`, `iv_rank`, `term_ratio`, `skew_25d`, `runup_1m_pct`, `runup_vs_sector_1m`, `dist_52w_high_pct`, `revision_direction_30d`, `short_interest_pct`, `regime_flag`), outcome (`beat_eps`, `beat_rev`, `guide`), reaction (`move_d1/d5/d20`, `coverage_ratio`), and management (`gate_path`, `action`, `pnl_final`). `ensure_v2()` adds the keys (null) idempotently and is wired into the IBKR import so every new trade carries the v2 shape; `schema_version=2` stamped on each row
- [x] **`backfill-trades` command:** links each trade to the screening run it came from (`find_screening_run`, matches both `screening_*` and `earnings_*` run dirs), fills T-1 context + `coverage_ratio` from the saved `pricing/crowding/asymmetry.json`, and fills `short_interest_pct` + outcome (`beat_eps`) + reaction (`move_d1/d5/d20`) from yfinance. Idempotent, guarded, `--no-network` / `--ticker` flags
- [x] Fields owned by later items stay explicit-null with a documented owner: `iv_rank`/`term_ratio`/`skew_25d` (#3b), `regime_flag` (#15), `gate_path`/`action` (#16). `revision_direction_30d` is 30d-based (yfinance's `eps_revisions` window) rather than the 90d the original spec named
- **Artifact divergence — RECONCILED (2026-06-24):** historical `reports/earnings/earnings_*` runs (produced by the **dashboard server**, `cli/server.py`) did not persist `pricing/crowding/asymmetry.json` because `server.py` had its *own copy* of the per-ticker screen loop that had drifted from `cli/commands/screen.py`. Fixed by extracting a single shared `screen_ticker()` (in `screen.py`) that both the CLI `screen` command and the server now call — it saves the complete report, `earnings_brief.md`, `fundamentals_score.json`, `peers.json`, and the `pricing/asymmetry/crowding.json` gate artifacts. Future dashboard runs are now fully enrichable. (Backfill still can't recover the T-1 context for *past* `reports/earnings/` trades — those files were never written — but it gets outcome/reaction/short-interest, and `find_screening_run` matches both layouts.)
- **Broader reports-layout split — RECONCILED (2026-06-24):** the older CLI commands used to write/glob `reports/screening_*` at the **repo root** while the dashboard/website/IBKR tooling used `reports/earnings/`, so they didn't see each other's runs (the CLI `calibrate`/`allocate`/`trades` found 0 of the user's 78 runs). Centralised on one helper, `tradingagents/reports_layout.py` (`runs_root()` → `reports/earnings`, `iter_run_dirs()` → all runs newest-first matching both `screening_*`/`earnings_*` prefixes + legacy root). Migrated every site: `screen.py` (output dir + resume), `allocate`, `calibrate`/`trades` listing, the two `server.py` calibration pickers, `reports.py` website, `calibrator.load_all_calibrations`/`list_uncalibrated_runs` (master JSON pinned to the repo root), `trade_reflections`/`reflect` run-name detection, and `allocation/layer._load_historical_scores`. CLI and dashboard are now interchangeable; calibration loaders that previously returned nothing now find all 20 calibrated + 57 uncalibrated runs
- [ ] Quarterly calibration loop: recompute hit rate by gate, regime, and run-up bucket; any gate whose pass-group doesn't beat its fail-group by a meaningful margin gets its threshold adjusted or cut (ties into #13 dream-mode)

### Notes
- Gated on trade volume — most recipes need a few dozen logged trades to mean anything
- The log schema should be built early (cheap) even before the backtests run, so data accumulates from now
- The reel's provocative companion ("you don't need to backtest") refers to live forward-testing/paper-trading as the ultimate validation — see #18's paper-trading loop

---

## #18 — Research-feed signal ideas (saved investing reels, reviewed 2026-06-23)

**Goal:** Capture the genuinely useful, on-strategy ideas surfaced by the ReelDigest "invest" collection (38 transcribed reels on `pochanpi`). Each item cross-references the section it enhances; ordered by value/effort. (Filtered out as off-strategy: intraday day-trading patterns, order-book square-root law, business-acquisition / brand / hospitality reels, and SaaS-product ads like Barebone.)

### External validation (no action — morale/strategic)
- Two reels independently name **TradingAgents**: one as a "top fast-growing AI-finance GitHub repo," another endorsing exactly our **three-layer** structure ("don't build a bot — build a research desk + agent framework + data layer, then execution; start in paper trading"). Our architecture direction is externally corroborated.

### High value / low effort
- [x] **Insider-signal refinement** ✅ DONE (2026-06-23) — `tradingagents/allocation/insider.py`. Deterministically detects **cluster buys** (≥3 distinct net-buying insiders in 90d) and **sell→buy reversals** (sold in the older window, buying now), plus notable buys and net values; routine/programmatic selling is labelled "often routine" and NOT treated as bearish. Persisted to `insider.json`, shown on the council `Insider:` line + a synthesis rule (cluster/reversal = bullish confirmation, can lift a tier; selling discounted). Live-validated: it independently flagged the medtech reel's own example (BSX cluster buy). Thresholds tunable for #17.
  - [ ] Follow-up: **SEC Form-4 (EDGAR)** as a more complete/timely source than yfinance (yfinance insider data can be laggy/partial).
- [ ] **Historical-relative valuation** in `fundamentals_scorer.py`: score valuation vs. the name's *own* historical median (the medtech reel: 17× EV/EBITDA vs. a 31× median), not just absolute metrics — cheap, strong context.
- [~] **Risk-adjusted stats** (also listed in #17): ✅ Sharpe / Sortino / MaxDD shipped 2026-07-18; turnover still open.

### Medium value — reinforce the regime/sizing layer (#15 / #6b)
- [ ] **Macro overlay can veto strong bottom-up signals** — the medtech reel is the canonical case: compelling insider buys + cheap valuation, but tariffs / reimbursement cuts / FDA slowdowns / policy make it un-underwritable. The #15 overlay should be able to *downgrade or veto* even strong scores on sector-level policy headwinds.
- [ ] **HMM market-regime classifier** (bull / bear / sideways from price + volatility) as a richer alternative to the hard VIX/SPX-50dma gate in #15 (RenTec-style; `QF-Lib` referenced).
- [ ] **Positioning data** — **CFTC COT** (free, weekly) for index/futures crowding and **dealer gamma** for index support/resistance/amplification, as market-level inputs to the regime + sizing layer (single-stock COT n/a, so this is a macro-layer signal).
- [ ] **Volatility-drag-aware sizing** — geometric growth = arithmetic − variance/2; high-vol names erode compounding. Reinforces the #15 implied-move sizing cap: penalise high-vol names beyond the implied-move loss cap alone.

### Medium value — other
- [ ] **Paper-trading / forward-test loop** (cross-ref #1, #16): explicitly recommended over backtesting alone. A mode that logs the council's recommendations as a shadow portfolio and scores them forward against real outcomes — the live-validation complement to #17's historical backtest.
- [ ] **Premium-selling structures** (cross-ref #5, and #14b's "route bad-asymmetry names to premium-selling" follow-up): **calendar spreads** for sideways / high-IV-crush names (profit from time decay instead of owning the binary event).
- [ ] **#12 OSS comparison set** (concrete candidates to benchmark/integrate): `AI-Trader`, `QuantDinger`, `daily_stock_analysis`, `Vibe-Trading`, `ai-hedge-fund` (virattt), `TradeMaster` (RL). The `ai-hedge-fund` repo's **investor-philosophy personas** (Graham / Buffett / Munger / Ackman) are an alternative to our generic council personas (Contrarian / First-Principles / …) worth A/B-ing.

### Low value / infra references (note only)
- [ ] A reel's full algo-trading blueprint suggests stack pieces we could adopt incrementally: **DuckDB** (fast file-based store vs. our JSON), **FMP** (alt fundamentals vendor vs. yfinance), **MLflow** (experiment tracking once many strategy variants exist), **Prefect** (orchestration for a scheduled daily fetch→score→allocate→log job — overlaps #13 dream-mode). Standard quant strategy families (momentum / mean-reversion / seasonality) and the 8-step ML workflow are noted for if we ever add a statistical signal layer alongside the LLM one.

### Notes
- Source: `/home/pochan/ReelDigest/summaries/*.md` on `pochanpi` (collection `invest`; web view `reeldigest.ochanis.in/c/invest` is auth-gated).
- Strongest single takeaway: the **validation methodology** (now folded into #17) directly answers the recurring "these thresholds are unvalidated priors" caveat on #14/#15.

---

## #19 — Conditional "Wait & Decide" Entries ✅ DONE (2026-07-18) — `tradingagents/allocation/watchlist.py`

**Goal:** A fourth council outcome — **WATCH** — alongside BUY/SHORT/SKIP, for names where the *business* is attractive but the *pre-print entry* is not (null EV, high fade rate, low coverage, heavy crowding, low/mid confidence). Instead of forcing a BUY-or-SKIP call, pre-commit a plan: skip the print, and if the stock sells off past a computed trigger level on results, buy the dislocation — quality names that gap down on an in-line print tend to rebound.

### Sub-tasks
- [x] Council output schema: `WATCH` direction with `trigger_price`, `watch_amount` (reserved from cash), `watch_expiry_sessions` (2–5); WATCH rows carry $0 and are exempt from the 6-position cap. Validator checks (`_check_watch_row`): amount must be 0, positive trigger/reserve, `fundamentals_score ≥ +2` quality gate, trigger below spot and ≥ 0.5 × implied move below it, Σ watch_amounts ≤ cash_reserved
- [x] Trigger guidance in the synthesis prompt: quality names on the soft-downgrade paths (EV n/a + fade/coverage, crowding, Low/Medium confidence), trigger ≥ 0.5 × implied move below spot, deeper when `E[move|miss]` is larger; "use sparingly — only names you'd own at the trigger"
- [x] `### Watchlist` section in `allocation.md` + WATCH fields in the Allocation Score JSON; entries persisted as **`watchlist.json` at the run root** (`build_watchlist`/`save_watchlist` in `layer.allocate`)
- [x] **Status lifecycle** (`entry_status`, pure + tested): PENDING (pre-print) → ARMED (in window, watching price vs trigger) → TRIGGERED (traded ≤ trigger inside window; hit detection uses session lows) → EXPIRED. Falls back to business-day counting when price data is unavailable
- [x] **Dashboard surfacing** (decided 2026-07-18: dedicated view + alert strip): new `Watchlist` sidebar view (sorted TRIGGERED → ARMED → PENDING, expired collapsed), nav badge counting only actionable entries (red when a trigger is hit), red alert strip on Overview when TRIGGERED, `/api/watchlist` endpoint recomputing live statuses (`collect_watchlists`), `/watchlist` deep link. Flag-only, manual execution
- [x] `backfill-trades` tags trades in WATCH-listed names with `strategy: "wait_and_decide"` so calibration can track the strategy separately
- [x] **Live `Now` price + row actions** (2026-08-10): `fetch_live_price()` (yfinance `fast_info`, 1-day close fallback, 60s TTL cache) replaces the stale daily close in the `Now` column and upgrades an ARMED entry to TRIGGERED intraday when it quotes at/below its trigger — trigger *detection* still uses session lows. Row-level **✓ Purchased** / **✕ Delete** buttons write a reversible user overlay (`reports/watchlist_state.json`, keyed `run_id|TICKER`, records the quote at purchase) via `POST /api/watchlist/action`; marked rows leave the nav badge and Overview alert and fold into Purchased/Dismissed sections. Still flag-only — the buttons record execution, they don't perform it

### Notes
- Fuses #10 (dislocation scanner) with #16's Gate-2 mechanical-drop logic, applied *proactively* at screen time to names we already analyzed — #10 remains useful for names we didn't screen
- Directly monetises the #14a finding that quality names sell good prints: instead of just sizing down, wait for the fade and buy it
- Decision (2026-07-18): flag-only v1; conditional IBKR orders would gate on #3b
- **Follow-ups:** WATCH-outcome calibration (did triggered entries rebound? did untriggered ones run away?) belongs to #1/#17; trigger-depth constant (0.5 × implied move) is a tunable prior in `watchlist.py`
- **Open (2026-08-10):** the `purchased` mark is bookkeeping only — it doesn't create a trade. Linking it to the IBKR import (match ticker + fill date to close the loop, so a bought WATCH row lands in `trades.json` with `strategy: "wait_and_decide"` automatically) is the natural next step

---

## #20 — Pair Trading / Peer-Hedged Entries (MEDIUM priority)

**Goal:** When conviction is name-specific but the sector/industry reaction is uncertain, offer a paired trade: long the screened name, short a correlated peer (or vice-versa for shorts). If the industry moves against us but our name outperforms the pair partner, the spread still pays.

### Sub-tasks
- [ ] Hedge-candidate pool from `earnings/peers.py`'s curated `PEER_MAP` (decided 2026-07-18: specific peer stock, not the sector ETF)
- [ ] Divergence heuristic: prefer a peer **not reporting within the same window** (avoid stacking two binary events), with correlated price history (≥ ~0.6 over 6m) but a *weaker* current setup (lower fundamentals/weighted score, worse peer read-through)
- [ ] Council integration: optional `hedge` field per allocation row (`{ticker, direction, amount}`); synthesis prompt rule for *when* to propose a pair (elevated crowding/sector risk + name-specific conviction) — suggestion, not default
- [ ] Validator: hedge legs respect the same caps; net sector exposure computed with hedges included
- [ ] `trades.json`: `paired_with` link field so combined P&L is evaluated as one position in stats/calibration
- [ ] Flag-only, manual execution (same as #19)

### Notes
- Most valuable exactly when #14c crowding or #9 `sector_bar_elevated` flags fire — gate the suggestion on those signals rather than always offering a pair
- Correlation needs ~6m of daily closes for both legs — cheap via the existing yfinance plumbing
- Sequenced after #16: exit management matters even more for two-legged positions

---

## Quick wins (no dedicated section above)

- [x] Add `--budget` flag to `tradingagents screen` so the $100k allocation budget is configurable at runtime
- [x] Clean up `screen.py` root script — updated to DeepSeek V4
- [x] Add sector to `trades.json` entries for future sector-level P&L analysis

---

## Operational — Job durability & resume ✅ DONE (2026-08-11)

**Trigger:** a dashboard-launched 149-ticker screen was killed at ticker 54 by a server restart (jobs run in the uvicorn process), and the UI showed nothing running while a detached recovery job worked for hours.

- [x] `cli/jobs_registry.py` — durable job list at `reports/jobs.json`: PID-derived liveness (dead process → `interrupted`), progress recounted from briefs on disk, atomic writes, 7-day pruning of finished records
- [x] `/api/jobs` merges in-memory jobs with the registry, so the activity banner shows work started by *any* process (detached CLI runs, jobs that outlived a restart); banner keyed by job id with live progress, and un-dismisses when new work appears
- [x] `cli/commands/resume.py` + `tradingagents resume` + `resume` job type + `/api/resume/candidates` / `/api/resume/plan`, surfaced as a **Resume** card in the Run view. Screens only missing tickers into the same folder via the shared `screen_ticker()`, retries started-but-empty folders first, rebuilds the table, re-runs allocation
- [x] Tests: 31 unit tests across `tests/test_jobs_registry.py` + `tests/test_resume.py`

- **Open:** jobs still die with the server — the registry makes that *visible and recoverable*, it does not make them survive. Real durability means running jobs in a subprocess (or refusing to restart while one is live). Long runs should be started detached until then.
- **Open:** `resume` cannot know the intended universe for `screening_*` runs with no calendar entry; it recovers only started-but-empty folders there. Persisting the submitted ticker list into `metadata.json` at launch would fix this for good.

---

## Operational — Dashboard load time ✅ DONE (2026-08-11)

**Trigger:** the dashboard had become unusably slow — 27 MB page, ~10 s to load.

- [x] Diagnosis: 72% of the payload was `earnings_brief_md` + `portfolio_decision_md` for all 3,072 ticker rows (19.7 MB), shipped up front but read one at a time
- [x] `_build_reports_data(limit_runs=, run_offset=, include_bodies=, limit_trades=, limit_reflections=, refresh_watchlist=)`; server sends 12 runs / 300 fills / 40 reflections / no bodies, `build-web` still embeds everything (no server behind a static page)
- [x] On-demand endpoints + **Load more** on Screenings, Analyses, Trades, Reflections: `/api/report` (one ticker's markdown, client-cached), `/api/runs`, `/api/trades?limit=&offset=`, `/api/reflections`
- [x] `refresh_watchlist=False` moves the live-quote round-trip off first paint (the page's own `/api/watchlist` call fills it in)
- [x] **27.8 MB / 10.4 s → 1.26 MB / 0.07 s warm** (1.26 s cold); 15 tests in `tests/test_reports_paging.py`
- [x] Resume buttons on every screening run card (preflight via `/api/resume/plan` before committing hours of work)

- **Open:** `/api/reflections` rebuilds the whole reflection list per page request; fine at 344, wasteful later
- **Open:** the Analyses view still derives from loaded runs, so its filters search only what's loaded — a server-side search endpoint would fix that properly

---

## Operational — Calendar render + market-cap filter ✅ DONE (2026-08-12)

- [x] Diagnosis: the Screenings calendar built every day's table up front — ~683 KB of HTML, 541 rows and 546 checkboxes across 5 days — all inside collapsed (`display:none`) day cards. `/api/calendar` itself was never slow (93 KB in 1.6 ms)
- [x] Day bodies now build on first expand (`calDayBodyHtml` + `data-built` cache): initial calendar paint **683 KB / 541 rows / 546 inputs → 3 KB / 0 / 0**, ~9 KB per day actually opened
- [x] Market-cap filter: min/max (bare number = $M, or K/M/B/T suffix), Micro/Small/Mid/Large presets, "include unknown cap" toggle, persisted in localStorage; header shows "31 of 168 companies"
- [x] `calDayTickers()` keeps "Screen Selected ▶" correct on a collapsed day — verified the fallback list is identical to the rendered checkboxes

- **Open:** the calendar filter is client-side over the whole fetched day; a server-side cap filter would matter only if a day ever ran to thousands of names

---

## Operational — Network-drop hang + resume universe ✅ DONE (2026-08-12)

**Trigger:** a laptop lost Wi-Fi mid-run. 44 of 84 tickers died as `ReadError: Connection reset by peer`, then the job wedged — 8 workers at 0% CPU, no progress for 34 min, the server still reporting "running".

- [x] Root cause: `httpx.Timeout(1800)` set *all four* timeouts, so a dead network burned a 30-minute **connect** timeout per attempt (× `DEEPSEEK_MAX_RETRIES`). Split out `DEEPSEEK_CONNECT_TIMEOUT_S = 20`, keeping the long read window that exists for silent reasoning phases. Applied to both the shared httpx pool *and* the per-request timeout — setting only one silently reinstates 1800s
- [x] Resume universe precedence fixed: `metadata.json["tickers"]` → run folders → calendar (opt-in via `--universe calendar` / `?universe=`). The calendar is a superset guess: a run of 84 hand-filtered tickers planned **128**, 84 of them never requested and all of them billable. `plan_resume` now reports `calendar_extra` so callers can offer the choice instead of making it
- [x] `_run_screen` records the submitted ticker list in `metadata.json`, so future runs never have to guess
- [x] "Resume failed: run not found" was a misleading error — a stale server 404s the *route*, not the run. The UI now distinguishes them by response body and says to restart
- [x] Tests: 21 in `tests/test_resume.py` (5 new for universe precedence); one pre-existing test was making a live API call once its fixture drifted — now mocked

---

## Fix — Overview showed partial lifetime P&L ✅ DONE (2026-08-12)

**Trigger:** after the payload work, the Overview reported $28k total P&L instead of $147k.

- [x] Two causes, both mine: `stats` was computed from the *truncated* `trades` list, and the Overview computes its tiles client-side from `DATA.trades` (which carried 300 of 1,262 fills)
- [x] `stats` now always computed over the full log (`all_trades`), independent of any display limit
- [x] `INITIAL_TRADES = None` — the trade log ships in full (~1 MB, vs the ~20 MB of report bodies that actually made the page heavy). Payload 1.26 → 1.77 MB, page still 0.07 s warm
- [x] 3 regression tests: totals, win rate, and that the `trades` array still honours its limit
- [x] Rule recorded in ARCHITECTURE §15: **a limit is safe for a list the UI displays, unsafe for one it aggregates**

---

## Fix — Streaming read timeout wedged runs for hours ✅ DONE (2026-08-13)

**Trigger:** third stuck run. 8/48 tickers done, then 3.5 h at 0% CPU with the job still reporting "running".

- [x] Diagnosed with `sudo py-spy dump` (needs root on macOS; `sample` too). All 8 workers blocked at the *same* line — `openai_client.py:90 invoke` → langchain `_stream` → `openai/_streaming.py` → httpx → `ssl.recv`. Six in `_run_graph`, two in `reflect_on_final_decision`
- [x] Root cause: **httpx applies the read timeout per read, so on a stream it is an inter-chunk gap, not a total budget.** `DEEPSEEK_TIMEOUT_S = 1800` was sized for the *non-streaming* first-byte case (silent reasoning); applied to a stream it let a dead network block a worker 30 min per attempt, × SDK retries ≈ the 3.5 h observed
- [x] `DEEPSEEK_STREAM_READ_TIMEOUT_S = 300` for streaming; 1800 retained for non-streaming. Timeout is now built *after* `streaming` is resolved — reading it before the `setdefault` always picked the non-streaming ceiling
- [x] Verified on a real client: `Timeout(connect=20, read=300, write=1800, pool=1800)` streaming, `read=1800` not
- [x] Red herring recorded: 73 of 94 sockets were Yahoo (43 ESTABLISHED, 30 CLOSE_WAIT) and only 1 DeepSeek, which pointed at yfinance. yfinance *does* pass `timeout=30`; the idle Yahoo sockets were keep-alives. **Socket counts show where connections are, not where threads are — dump stacks first**

- **Open:** `DEEPSEEK_MAX_RETRIES = 5` multiplies any timeout; worst case is now ~25 min rather than ~3 h, but a per-ticker deadline would bound it properly
- **Open:** a job whose workers are all blocked still reports "running" — the registry could flag "no progress in N min" as stalled

---

## Fixes — Screenings page: blocked loop, run order, calendar index ✅ DONE (2026-08-13)

- [x] **"Calendar takes 5 s"** was not the calendar (93 KB in 2 ms). Every heavy endpoint was `async def` with a synchronous body, so it ran *on the event loop* rather than in Starlette's threadpool; a cold `/api/watchlist` (~8 s of yfinance quotes) stalled every other request behind it. 19 handlers converted to `def`. Measured: `/api/calendar` during a cold watchlist **5 s → 0.005 s**
- [x] Watchlist quote warm-up now runs concurrently (8 threads) instead of one round-trip per ticker: 7.9 s → 3.7 s, and no longer on the critical path
- [x] **Run ordering:** `iter_run_dirs` sorted by raw name, so `screening_*` outranked `earnings_*` ('s' > 'e') whatever the date — five months of August runs sat below May ones, and since the dashboard loads only the newest page, the calendar could match none of them. New `run_sort_key()` sorts by (label date, run timestamp)
- [x] **Calendar independent of paging:** new `/api/screening-index` (`build_screening_index`) indexes *every* run, scoped to calendar tickers and cached against the run set — 126 KB, 0.2 s cold / 5 ms cached. A ticker's screening history now shows whether or not its run has been loaded
- [x] Tests: 10 in `tests/test_run_ordering.py`; `test_reports_layout` updated — it asserted the buggy "newest-first by name" contract and its own fixture reproduced the bug

- **Open:** the first payload grew 1.77 → 3.6 MB now that the newest 12 runs are the *big* August ones (up to 149 tickers each). Page is still 0.2 s warm, but `screening_table_md` + `allocation_md` per run (~140 KB each) are the bulk and could be lazy-loaded on expand, exactly like calendar day bodies

---

## Fix — Stuttery load: content arriving after first paint ✅ DONE (2026-08-13)

**Trigger:** "there is a first load where most of the content gets loaded and then after like a second some other content gets loaded… the armed/pending load quickly but the triggered takes like a second."

- [x] **Watchlist.** The symptom was exact: TRIGGERED is the only status needing a live quote, and the payload was built with `refresh_watchlist=False`, so those rows painted as ARMED and flipped when `/api/watchlist` landed. The Overview's alert strip — the most actionable thing on the page — was always late for the same reason
- [x] Real cost was not the quotes but **one sequential yfinance `history()` call per entry** (~150 ms each) for the trigger scan. New `fetch_bars()` with a 300 s TTL cache, warmed concurrently alongside quotes, and only for rows that actually get a live refresh (dismissed and stale rows were being fetched for nothing). `collect_watchlists` cold 6.7 → 1.4 s, warm 2.3 s → **0.01 s**
- [x] Server keeps a **stale-while-revalidate snapshot** (`watchlist_snapshot()`, 45 s TTL, background refresh, warmed at startup, republished after `/api/watchlist/action`). `/` embeds it, so the first paint is already live-accurate. `/api/watchlist` **2.3–6.7 s → 1 ms**
- [x] Frontend repaints only on a real change (`wlSig()`), and the alert sits in a stable `#wl-alert-slot` (`wlPaintAlert()`) — adding that one strip used to re-run `renderOverview()` and rebuild every chart under it. Verified: the boot fetch is now byte-identical to the embed and repaints nothing
- [x] Overview alert now also refreshes on a 60 s poll, so a trigger firing surfaces without a reload — affordable only because the endpoint is 1 ms
- [x] **Checked what else was affected, and found the bigger one:** `renderScreenings()` re-fetched all of `/api/data` (**3.2 MB, 1.9 s**) on *every* visit, just to notice runs finished since load. Replaced with `_scrCheck()` over `/api/screening-runs` (11 KB, 40 ms) as a change probe; the heavy `/api/runs` pull happens only when its signature moves. The two job-completion handlers that also pulled `/api/data` now use the same light path
- [x] `/api/screening-runs` had the same name-sort bug fixed earlier in `iter_run_dirs` — now uses it (also picks up legacy root runs)
- [x] `_pollJobs()` fires immediately instead of at 600 ms, so the activity banner is there on first paint rather than popping in
- [x] Tests: 16 in `tests/test_watchlist_caching.py` (bar-cache TTL/keying/failure, fetch scoping, real concurrency — mutation-checked by forcing `max_workers=1`, snapshot staleness and single-flight). An existing test caught a genuine cross-test cache leak → `clear_caches()` + autouse fixture. 351 unit tests pass
- [x] Rule recorded in ARCHITECTURE §15: **after the first paint, fetch to compare, not to replace**

- **Open:** first payload is still 3.6 MB (see previous entry) — unchanged by this work, since the fix removed a *repeat* download rather than the initial one

---

## Feature — Stop a running job from the dashboard ✅ DONE (2026-08-13)

**Trigger:** "one job got stuck and I could resume it. However, the stuck job stayed — any chance we can add a button to stop a job when I know it is stuck?"

- [x] **The constraint that shaped it:** dashboard jobs are threads *inside* the uvicorn process, so `jobs.json` records the server's own PID for them (confirmed on the user's two stuck jobs: both `pid=27024`, the server). A stop that signals the PID would kill the dashboard. Cancellation is therefore cooperative, and the code says so where someone might be tempted to "fix" it
- [x] `jobs_registry`: `request_cancel()` / `is_cancelled()`, statuses `cancelling` → `cancelled`, `IN_FLIGHT` / `TERMINAL` groupings. Intent lives in an in-memory set *and* the file, so a detached CLI run can be stopped from the dashboard
- [x] Workers check between tickers: queued tickers never start, in-flight ones finish, allocation council is skipped on a partial run while `screening_table.md` is still written so **Resume** can finish it
- [x] `POST /api/jobs/{id}/stop` branches on where the work lives — cooperative for this process, SIGTERM/SIGKILL(`force`) for another live one, and for a process already gone it *persists* the derived terminal status (`interrupted` is computed at read time and was never written, so a stuck record kept coming back as "running" — that is the bit the user actually hit)
- [x] UI: **■ Stop** in the activity banner; with more than one job in flight it opens a picker rather than guessing. Copy states plainly that in-flight tickers finish first and that partial work is kept. `cancelling` stays on the banner as "stopping" rather than vanishing
- [x] Tests: 20 in `tests/test_job_stop.py`, incl. the one that matters — *stopping an own-process job sends no signal*. Two real bugs surfaced while writing them: stopping before any brief existed filed the run as `ERROR` ("no parseable briefs") instead of cancelled, and an already-dead job's terminal status was not persisted
- [x] Live-verified against the running server: unknown job → 404; synthetic dead-PID record → cleared and no longer reported in flight. 371 unit tests pass

- **Open:** a job wedged inside a single `screen_ticker()` call still takes until the DeepSeek timeout (~5 min, or ~25 min across retries) to actually let go. Bounding a ticker with a deadline is the real fix; Stop makes it visible and stops it growing

---

## Fix — Portfolio returns divided by turnover, not capital ✅ DONE (2026-08-14)

**Trigger:** "the MWR is dividing by 15.7MM, the sum of each time I deployed capital. This trades many times but the capital deployed is ±600K redeployed every time, shouldn't that be the denominator?" — correct, and it understated the account by ~19×.

- [x] **MWR** was `Σ P&L ÷ Σ notional`. Σ notional counts redeployed capital once per round trip, so a book turning over ~19× reported **+0.99%** on $15.7M traded for a period that actually returned ~**+18%** on ~$840k
- [x] **TWR** was `Π(1 + pnl/cost)` over *every trade*. Trades are concurrent (median 15 exits/day), so chaining 1,322 of them compounded each position onto the last: **+1,580%** cumulative, **+1,515,750%** annualized. Now chain-links *daily* returns on the capital base
- [x] **Capital base** = largest notional exiting on any one day (`dashCapitalBase`). Entry dates are absent (IBKR Flex closing records omit the open date; `trade_date` null on all 1,322 fills), so exposure cannot be integrated over time — but trades exiting together were provably open together, making this a hard lower bound. Errs low: understates the return, never inflates it. Chosen by the user over a manual capital setting or an IBKR Flex-query change
- [x] Annualization guarded for `r ≤ −1` (was capable of `NaN`), and the panel's meta line now names the base, its peak day and the turnover — the two numbers whose conflation caused the bug
- [x] Fixed in **both** dashboards; the legacy `dashboard.html` had the identical bug plus a comment asserting it was "equivalent to Modified Dietz", which it was not
- [x] Result on live data: TWR **+1580.98% → +19.81%** (ann. +1,515,750% → +85.23%), MWR **+0.99% → +18.46%** (ann. +3.40% → +78.24%)
- [x] Tests: 15 in `tests/test_portfolio_returns.py`, run under node against the functions extracted from the real HTML (no Python reimplementation to drift), incl. a cross-check that the two dashboards agree. Mutation-verified: restoring the Σ-notional denominator fails 5, restoring per-trade chaining fails 2. 386 unit tests pass

- **Open:** the base is a *bound*, not the true average exposure. Adding `openDateTime` to the IBKR Flex query would let `trade_date` populate and allow real average capital employed (`Σ notional×days ÷ period`); the call sites already take a single `base`. Flex's 30-day window means the 1,322 historical fills stay uncovered either way

---

## Investigation — IBKR entry dates need Closed Lots, not just the field ✅ DONE (2026-08-14)

**Trigger:** "I added some fields in the IBKR query, can you check if this will allow to get the open trade times?"

- [x] **Answer: no, not on its own.** Fetched a fresh statement (query 1495116, 786 trades). `openDateTime` is now emitted on **786/786** rows and populated on **0** — including all 360 closing STK trades. `levelOfDetail` absent on every row, no `<Lot>` children
- [x] Cause: `openDateTime` is a **closed-lot** field. An execution does not know which lot it closed, so an executions-only query emits the column empty. The Trades section must also produce the **Closed Lots** level of detail
- [x] **Trap closed before it could fire:** lot rows repeat `openCloseIndicator="C"` *and* `fifoPnlRealized`, while `parse_closing_trades` kept every `<Trade>` with `openCloseIndicator="C"` — so enabling Closed Lots would have booked every trade's P&L twice. The import dedupes only against *already-stored* ids, not within a batch, and the Pi auto-imports unattended every ~15 days, so this would have corrupted `trades.json` with nobody watching
- [x] `_is_lot_row()` / `_lot_open_dates()`: lot rows now contribute **dates only**, matched by trade/exec id, earliest lot winning when one close consumes several. Handles both shapes (`levelOfDetail="CLOSED_LOT"` and nested `<Lot>`)
- [x] Import gained intra-batch dedupe by `ibkr_trade_id` as a second line of defence
- [x] Verified no behaviour change on the current statement: 360 trades parsed, 0 entry dates — identical to before
- [x] Tests: 7 in `tests/test_ibkr_flex.py`. Mutation-checked: removing the lot-row skip fails 4. 393 unit tests pass

- **Next:** user enables Closed Lots in the Flex query → re-import populates `trade_date` → the returns panel can move from the peak-exposure *bound* to true average capital employed. Flex's ~30-day window still means the 1,322 historical fills stay uncovered

---

## Investigation — 365-day Flex window: entry dates still blocked ✅ DONE (2026-08-14)

**Trigger:** "I just changed the flex query to include 365d instead of 30d. can you try to do this backfill once and moving forward it will work?"

- [x] The window change worked: statement now spans **2025-08-14 → 2026-08-13**, 4,201 trades (was 786 over 30d)
- [x] **Entry dates are still blocked.** `openDateTime` populated on **0 of 4,201** rows; `levelOfDetail` absent everywhere; zero `<Lot>` elements. The period is not what gates this — the **Closed Lots** level of detail in the Trades section is, and it is still off. "Moving forward it will work" is not yet true
- [x] Attempted a reconstruction instead: FIFO-match opening executions to closes (the wider window finally includes the opens). **Validated against IBKR's own `fifoPnlRealized`** — the honest check, since reproducing their realized P&L proves the lot matching. First pass 12.4%; driving the match off `openCloseIndicator` and ordering closes before opens within a shared timestamp got it to **24.2%**. Not good enough — **not written to the trade log**. Reconstructed dates would have fed both the returns panel and the reflection layer
- [x] Why it fails: heavy same-second activity (one close plus four opens at an identical `dateTime`), `C;O` reversal rows, and positions opened before the window start. IBKR's lot selection cannot be reliably re-derived from execution rows alone — which is precisely why the closed-lot rows exist
- [x] **Real win from the wider window:** the import would add **539 closing trades** the canonical log is missing, back to 2025-10-09 (+$16,926 P&L). Offered, not run — it moves the headline P&L and the import's ticker filters are the user's call
- [x] Doc drift fixed: the trade log is `reports/trades.json` (`_trades_path()` migrated it once); CLAUDE.md and ARCHITECTURE.md still pointed at `~/.tradingagents/trades.json`, now a stale 389-trade copy

- **Next:** enable **Closed Lots** in the Flex query's Trades section → re-import → `trade_date` populates → returns panel can use true average capital employed instead of the peak-exposure bound

---

## Fix — half the API keys were silently ignored ✅ DONE (2026-08-20)

**Trigger:** after defaulting "Screen Selected" to 16 workers, the user said "I think I have 16 workers, can you check the .env?"

- [x] `.env` holds **16** `DEEPSEEK_API_KEY*` values; `gather_api_keys()` returned **8**. The suffix list was hard-coded `["", "_2" … "_8"]`, so keys 9–16 had never been used by any run
- [x] Consequence, not cosmetic: screening assigns one key per worker (`api_keys[idx % len(api_keys)]`). Every 16-worker run was putting **two concurrent streams on each of 8 keys** — indistinguishable from a rate limit, and a plausible contributor to the `ReadTimeout` clusters that wedged the August runs
- [x] Replaced with an unbounded scan of the environment (regex on the provider's base var), numerically ordered, gaps tolerated, blanks/duplicates skipped, non-numeric suffixes like `_BACKUP` ignored, and no false match on a longer var such as `AZURE_DEEPSEEK_API_KEY`
- [x] The existing test *passed* against the bug because it cleared only slots 1–8 and let the real environment leak in; it now clears the whole namespace. 7 new cases; mutation-checked by reinstating the `_8` ceiling → 3 fail. 399 unit tests pass
- [x] Undocumented feature, now written down in `.env.example` and CLAUDE.md: **key count is the natural ceiling on worker count**
- [x] Same change: "Screen Selected" defaults Workers to 16 (`sm-workers`, `sc-workers`, and `screenCalendarDay()`); `max` raised 8 → 16 or the browser clamps the value

---

*Last updated: 2026-09-05*
