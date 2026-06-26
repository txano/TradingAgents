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
3. **#15 — Implied-move sizing cap + tactical regime gates** ⟐ (free; deterministic loss control; `learn` repeatedly asks for a macro risk-off overlay) ← NEXT
4. **#10 — Post-earnings dislocation scanner** (free; a second, independent trade source)
5. **#1 — Calibration extensions + `suggest-weights`** (turns the above into measured, self-tuning gates)
6. **#16 — Trade management: exit map + post-earnings triage** ⟐ (high P&L leverage; the single most-repeated `learn` process note — wins closed too early; needs live position tracking)
7. **#17 — Backtest harness** (validates every gate/threshold on your own log; gated on trade volume)
8. **#6b — Shiller CAPE / Buffett indicator** (cheap structural regime context)
9. **#11 — Multi-model ensemble** · **#3b — IBKR options pipeline** (IV rank / term structure / skew, once free signals are mined)
10. Everything else (#4 sector memory, #5 options structures, #6a macro scraping, #7 social, #8 Hermes, #12 OSS analysts, #13 dream-mode)

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

## #15 — Implied-Move Position Sizing & Tactical Regime Gates (HIGH priority, free)

**Goal:** Let the implied move (not conviction alone) set position size, and cut sizing in hostile regimes. Deterministic loss control that drops into `allocation/validator.py` and the council sizing rules.

> **Corroborated by `learn` (2026-06-16):** the self-improvement loop independently surfaced a **macro risk-off overlay** as a recurring process note across two runs (reduce size on yield spikes / geopolitical / oil shocks — cited losses: DLO, BKNG, GM, AG, REZI, OXY). It also recommended volatility-adjusted sizing. These can't be auto-applied as prompt edits, confirming the regime-gate work below is real and evidence-backed, not speculative.

### 15a — Implied-move sizing cap
- [ ] Add a hard cap so any single position's *implied-move loss* (`size × implied_move`) never exceeds a fixed % of NAV (playbook suggests 0.75–1.0% on a 1.8–1.9× levered book)
- [ ] Effect: a ±10% implied-move name gets half the shares of a ±5% name — "sizing off the implied move, not conviction alone, is what keeps one bad print from dominating the month"
- [ ] Enforce in `validator.py` alongside the existing 30% position / 35% sector caps

### 15b — Tactical regime gates (distinct from #6b structural CAPE/Buffett)
- [ ] **Risk-off gate:** SPX below its 50-dma or VIX > 22 → halve all earnings position sizes (in risk-off tape, beats get sold)
- [ ] **Macro-collision gate:** FOMC/CPI within ±2 sessions of the print → halve again or skip (the reaction will be contaminated)
- [ ] Inject regime flags into the council synthesis prompt and apply the multiplier in sizing

### Notes
- VIX/SPX/50-dma and an econ-calendar check are cheap (yfinance + a static FOMC/CPI date list); no IBKR needed
- #6b (Shiller/Buffett) is the slow structural governor; this is the fast tactical one — they stack

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
- [ ] **Run-up test:** bucket past trades by 1-month pre-print run-up (quartiles); hit rate + avg P&L per bucket. Prediction: losers cluster in the top run-up quartile → validates #14c and sets the threshold empirically
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
- [ ] **Risk-adjusted metrics** in `stats`: add Sharpe, Sortino, MaxDD, hit-rate, turnover (we currently only show win-rate / avg P&L)
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
- [ ] **Insider-signal refinement** (cross-ref: fundamentals analyst, which already has `get_insider_transactions`). The high-signal patterns are **cluster buys** and **sell→buy reversals** (insiders who stop selling and start buying), *not* any insider activity — two reels corroborate this with real forward-return claims (medtech: BSX/ABT/GEHC; Toyota→Joby). Detect those patterns specifically; consider **SEC Form-4 (EDGAR)** as a more complete/timely source than yfinance.
- [ ] **Historical-relative valuation** in `fundamentals_scorer.py`: score valuation vs. the name's *own* historical median (the medtech reel: 17× EV/EBITDA vs. a 31× median), not just absolute metrics — cheap, strong context.
- [ ] **Risk-adjusted stats** (also listed in #17): Sharpe / Sortino / MaxDD / hit-rate / turnover in the `stats` command.

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

## Quick wins (no dedicated section above)

- [x] Add `--budget` flag to `tradingagents screen` so the $100k allocation budget is configurable at runtime
- [x] Clean up `screen.py` root script — updated to DeepSeek V4
- [x] Add sector to `trades.json` entries for future sector-level P&L analysis

---

*Last updated: 2026-06-24*
