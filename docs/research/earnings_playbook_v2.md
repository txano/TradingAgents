# Earnings Trade Playbook v2
### Pre-earnings scorecard · Outcome-dependent exit map · Post-earnings triage · IBKR options pipeline · Backtest recipes

---

## Part 1 — Pre-earnings Scorecard v2

Your current system answers: *"Will they beat and raise?"* This layer answers the question that actually pays: *"Is my expected outcome better than what's already priced in, and is the payoff asymmetry in my favor?"*

Run the scorecard in four blocks. Blocks A and C produce numbers; Blocks B and D produce gates.

### Block A — The Expectations Gap (new, highest priority)

| Metric | How to compute | Signal |
|---|---|---|
| **Implied move** | ATM straddle mid-price ÷ spot, for the first expiry *after* the earnings date (see toolkit script) | The market's priced-in magnitude. Your predicted surprise must clear this bar, not zero. |
| **IV rank** | Current 30d IV vs. its own 52-week range: (IV − min) / (max − min) | IV rank > 80%: market braced for fireworks; beats get sold. IV rank < 30%: complacency; surprises travel further. |
| **Event premium (term structure)** | Front-expiry IV ÷ next-monthly IV | Ratio > 1.4: heavy event speculation, violent IV crush, gap likely overdone in both directions. |
| **Skew** | 25Δ put IV − 25Δ call IV | Steep put skew = hedged longs (downside cushioned). Call skew richer than usual = speculative call buying = squeeze fuel but also fade risk on "merely good" results. |

**Decision rule:** Estimate your expected post-print move (from Block C history conditioned on your beat/raise prediction). If `|expected move| < 0.6 × implied move`, the trade is structurally poor even if your beat call is right — skip or trade it via spreads that sell the inflated IV instead of owning stock into the print.

### Block B — Positioning (new gates)

1. **Run-up:** 1-month and 3-month return vs. sector ETF. Run-up > +12% absolute or > +8% vs. sector in the month before = beat is substantially priced. This is the single best predictor of "beat-and-fall" (your PANW/CRDO/VEEV pattern).
2. **Distance from 52-week high:** Within 3% of the high into a print = asymmetry against you (limited upside surprise room, large air pocket below).
3. **Revision momentum:** Direction of consensus EPS revisions over the prior 90 days. Estimates revised *up* into the print = the whisper number sits above consensus = your "beat" prediction must clear the whisper, not the published consensus. Treat 3+ upward revisions in the final month as raising your required surprise by roughly one notch.
4. **Short interest:** SI > 10% of float + your model says beat = squeeze amplifier (size up modestly). SI > 10% + uncertain = stay out; both tails are fat.
5. **Peer read-through:** Did same-sector companies that reported earlier this season beat and *still* fall? If yes, the sector bar is elevated regardless of the individual name — downgrade conviction one notch.

### Block C — Payoff Asymmetry (new, run per ticker)

From the stock's last 8 quarterly prints, compute:

- `E[move | beat]` — average day-1 reaction when EPS+revenue beat
- `E[move | miss]` — average day-1 reaction on a miss
- **Fade rate** — % of beats followed by a *negative* day-1 close
- **Coverage ratio** — average |actual move| ÷ implied move (stocks that habitually exceed their implied move reward owning the event; stocks that stay inside it reward selling premium)

Then: **EV = P(beat) × E[move|beat] + (1 − P(beat)) × E[move|miss]**, where P(beat) is your system's output.

**Decision rule:** Only take the long if EV > 0 *and* EV ÷ implied move > 0.25. High-multiple names routinely show E[move|miss] of −12% vs E[move|beat] of +4% — at those asymmetries, even a 70%-accurate beat model has negative expectancy. This check alone would filter most of the painful trades.

### Block D — Regime (gates)

- SPX below its 50-dma or VIX > 22 → halve all earnings position sizes; in risk-off tape, beats get sold.
- Macro collision (FOMC/CPI within ±2 sessions of the print) → halve again or skip; the reaction will be contaminated.
- Earnings season phase: late-season prints in a sector that already disappointed face a raised bar.

### Position sizing

`size = base_size × conviction(model score) × EV_multiplier × regime_multiplier`, with a hard cap so that any single pre-earnings position's *implied-move loss* (size × implied move) never exceeds a fixed % of NAV — on a 1.8–1.9x levered book, suggest 0.75–1.0% of NAV as the cap. This means the implied move itself sets the size: a ±10% implied move name gets half the shares of a ±5% name. Sizing off the implied move, not off conviction alone, is what keeps one bad print from dominating the month.

---

## Part 2 — Outcome → Action Map ("depends on outcome", formalized)

You said the holding period depends on the outcome. Good — but decide the mapping *now*, while unemotional. Print this table; the post-print decision should take 60 seconds of classification, not an evening of agonizing.

| # | Outcome | Day-1 price action | Action | Horizon |
|---|---|---|---|---|
| 1 | Beat + raise | Gaps up, **holds/closes near highs on volume** | **Hold, ride the drift.** Trail stop below day-1 low. | 2–8 weeks |
| 2 | Beat + raise | Gaps up, **fades to flat/red intraday** | **Take the gap.** Sellers used your liquidity; the event trade is over. Exit ≥75%. | Done day 1–2 |
| 3 | Beat + raise | **Gaps down** (mechanical / positioning unwind) | **Hold, do NOT add day 1.** Go to Triage (Part 3). | Decide day 3–5 |
| 4 | Beat EPS, soft revenue or margin compression | Any | **Trim 50% into any strength**, exit rest within 5 sessions. Beat quality failed. | ≤1 week |
| 5 | Beat quarter + flat/lowered guide | Any | **Exit.** Guidance outranks the quarter. The thesis the market trades is forward. | Day 1, into the first bounce |
| 6 | Miss | Any | **Exit day 1. Never average down.** Re-entry is allowed only as a *new* trade after a multi-week base. | Immediate |
| 7 | In-line everything | Stock down on IV-crush drift | Exit; you had no edge, paid event risk for nothing. Log it as a scorecard miss. | Day 1–2 |

Two structural notes baked into rows 1, 3 and 6:

- **Drift is your friend when long, your enemy when wrong.** Large negative surprises tend to keep underperforming for 60–90 days as the market digests; large positive ones keep drifting up. Day-1 direction is information, not noise. (Caveat: in mega-cap liquid names the day-1 move is mostly final and drift is weak — so rows 1 and 6 matter most for mid-caps and high-growth names; for the AAPL/MSFT tier, treat day 1 as the whole game.)
- **Row 6 is absolute.** A miss means your model *and* the company were wrong. Averaging into that on leverage is the one trade type that can break the book.

---

## Part 3 — Post-Earnings Triage (you're down: sell / hold / double)

Run the gates **in order**. The answer is never derived from the size of the loss — only from the diagnosis.

### Gate 1 — Thesis audit (night of the print)
Decompose the report into three verdicts: **Quarter** (beat/miss, and quality — revenue, margins, one-time items), **Guide** (raised/maintained/cut), **Narrative** (did the call commentary undermine why you own it — competition, demand, margins?).

- Any of: miss, guide cut, narrative break → **SELL.** Stop here. Use the first bounce/stabilization within 1–2 sessions to exit, but exit. Do not proceed to Gate 2 looking for a reason to stay.
- Quarter + guide + narrative all intact → proceed.

### Gate 2 — Mechanical-drop diagnosis (night of the print)
Evidence the drop is positioning, not information — check all that apply:
- ☐ Stock ran > 10% into the print (longs taking profit)
- ☐ Actual move ≤ implied move (the result was "as expected"; this is de-risking, not repricing)
- ☐ Results genuinely beat the *whisper* (revisions were flat into the print, so consensus ≈ whisper)
- ☐ Sector peers fell on similar good prints this season (sector-wide bar, not company news)
- ☐ Volume profile: heavy at the open, drying up into the close (unwind exhausting) — vs. building all day (institutions still leaving)

3+ boxes → mechanical, proceed to Gate 3. ≤2 boxes → the market knows something; **SELL half now, half on any 2-day bounce.**

### Gate 3 — Let price confirm (days 2–5; this gate exists to stop you from catching the knife)
Do nothing for 2–5 sessions. Then classify:
- **Reclaim:** closes back above the post-earnings open, or reclaims the pre-earnings level, on decent volume → proceed to Gate 4. 
- **Base:** holds the day-1 low, volume contracting → **HOLD**, set a time stop: if no repair within 15 sessions, exit — flat money on a levered book has a real cost, and a stock that can't bounce off a "mechanical" drop wasn't mechanical.
- **New lows on days 2–5:** the drift is in control → **SELL.** Mechanical drops don't make new lows for a week.

### Gate 4 — The add (only reachable with thesis intact + mechanical diagnosis + price confirmation)
- Add a **pre-defined increment: max 50% of the original position** ("double" as a word, never as 2x the exposure on a levered book).
- Hard stop on the *entire combined position* at the post-earnings low. The add's thesis is "the low is in"; if the low breaks, the thesis is wrong and everything goes.
- Gross-exposure check first: if the add pushes book leverage past your ceiling, something else gets trimmed *before* the add, not after.
- One add per name per quarter. No re-adding if stopped.

**The honest summary of the whole tree:** SELL is the answer at three of four gates. HOLD requires passing two. ADD requires passing all four plus a sizing and leverage check. That asymmetry is deliberate — it mirrors the asymmetry of the payoffs.

---

## Part 4 — IBKR Options Pipeline Spec

What to pull, per candidate ticker, T-3 to T-1 before the print (companion script: `earnings_toolkit.py`):

1. **Option chain** via `reqSecDefOptParams` → expirations + strikes.
2. **Event expiry** = first expiration ≥ earnings date.
3. **ATM straddle**: strike nearest spot; mid of call + mid of put; **implied move = straddle ÷ spot**.
4. **IV** via market data generic tick 106 (option implied volatility) on the ATM options; store daily to build your own IV-rank history (IBKR won't hand you 52 weeks of IV history on the cheap — start logging now, it self-builds in a few months; in the interim the script approximates IV rank from the underlying's realized-vol range).
5. **Skew**: IV at the strike ≈ 25Δ put vs ≈ 25Δ call on the event expiry.
6. **Term structure**: ATM IV event expiry ÷ ATM IV next monthly.
7. Persist everything to the trade log (schema below) — including, after the print, the **actual move**, so coverage ratio per ticker accumulates automatically.

Run it through your existing TWS/IB Gateway session (paper or live port). The script uses `ib_async` (maintained successor of `ib_insync`).

---

## Part 5 — Log Schema v2 + Backtest Recipes

### Columns to add to your existing log

| Column | When filled | Purpose |
|---|---|---|
| `implied_move_pct` | T-1 | Expectations bar |
| `iv_rank`, `term_ratio`, `skew_25d` | T-1 | Positioning context |
| `runup_1m_pct`, `runup_vs_sector_1m` | T-1 | Crowding |
| `dist_52w_high_pct` | T-1 | Asymmetry |
| `revision_direction_90d` | T-1 | Whisper proxy (up/flat/down) |
| `short_interest_pct` | T-1 | Squeeze/crowding |
| `regime_flag` | T-1 | SPX>50dma & VIX<22 → "on", else "off" |
| `beat_eps`, `beat_rev`, `guide` | T+0 | Outcome decomposition (bool, bool, raise/maintain/cut) |
| `move_d1`, `move_d5`, `move_d20` | T+1..T+20 | Reaction + drift |
| `coverage_ratio` | T+1 | `abs(move_d1)/implied_move` |
| `gate_path` | T+1..T+5 | Which triage gates fired (e.g. "G1-pass,G2-mech,G3-base,HOLD") |
| `action`, `pnl_final` | Close | What you did, what it made |

### Backtests to run against your existing detailed log (in priority order)

1. **The run-up test:** bucket past trades by 1-month pre-print run-up (quartiles). Hit rate and average P&L per bucket. Prediction: your losers cluster in the top run-up quartile. If confirmed, the Block-B gate is validated *on your own data* and you can set the exact threshold empirically instead of using my 12%.
2. **The implied-move test** (retroactively approximable: use the day-1 move of the *previous* 4 quarters as a proxy where you didn't record implied move): how often did your wins exceed the typical implied move? If rarely, your edge has been direction-only and the EV filter (Block C) will add the most value of anything in this document.
3. **The averaging-down counterfactual:** for every losing trade, simulate the Part-3 tree against what actually happened (you have the dates; pull the price paths). Compare: actual P&L vs tree P&L vs naive double-down P&L. This converts the framework from "plausible" to "proven on my book" — or falsifies parts of it, which is just as valuable.
4. **The guide-dominance test:** P&L split by `guide` outcome regardless of beat. Prediction: guide direction explains more of your day-1 P&L variance than the beat itself. If true, re-weight your model so guidance prediction outweighs beat prediction.
5. **Per-ticker fade rate:** for repeat names in your log, compute the Block-C stats. Some of your universe will reveal itself as "never own this one into the print, always wait for the dip."

### Quarterly calibration loop
Every quarter-end: recompute hit rate by gate, by regime flag, by run-up bucket. Any gate whose pass-group doesn't outperform its fail-group by a meaningful margin gets its threshold adjusted or gets cut. The playbook is a living object; the log is its training data.

---

*This is a decision framework, not personalized investment advice — calibrate every threshold against your own log and risk limits before sizing real capital on it.*
