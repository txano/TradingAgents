"""AllocationLayer — allocates a fixed budget across screened tickers."""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from tradingagents.allocation.analyst import create_allocation_analyst
from tradingagents.allocation.common import cut as _cut, parse_allocation  # noqa: F401 — parse_allocation re-exported for backward compat
from tradingagents.allocation.council import run_council
from tradingagents.allocation.fundamentals_scorer import fetch_fundamental_metrics, score_fundamentals
from tradingagents.allocation.pricing import fetch_pricing_context, format_pricing
from tradingagents.allocation.asymmetry import build_asymmetry, format_asymmetry
from tradingagents.allocation.crowding import fetch_crowding, format_crowding
from tradingagents.earnings.peers import build_peer_readthrough, format_peer_oneliner
from tradingagents.allocation.weights import apply_weights, load_weights
from tradingagents.learning.lessons import distill_lessons, load_cached_lessons, load_reflections

logger = logging.getLogger(__name__)


class AllocationLayer:
    """Allocates a fixed capital budget across a batch of screened tickers.

    Uses an AI council by default: five advisors with different analytical
    styles review all tickers in parallel, cross-review each other, then a
    synthesis pass produces the final allocation.

    Usage:
        layer = AllocationLayer(llm=ta.deep_thinking_llm, budget=100_000)
        report = layer.allocate(
            results=sorted_results,
            trade_date="2026-05-01",
            screening_dir="reports/screening_2026-05-01_...",
            save=True,
        )
    """

    def __init__(
        self,
        llm,
        budget: int = 100_000,
        use_council: bool = True,
        reflections_dir: str | Path = "reports/reflections",
        reports_dir: str | Path = "reports",
        advisor_llms: list | None = None,
    ):
        self.llm = llm
        self.budget = budget
        self.use_council = use_council
        self.reflections_dir = Path(reflections_dir)
        self.reports_dir = Path(reports_dir)
        self.advisor_llms = advisor_llms

    def allocate(
        self,
        results: list[dict],
        trade_date: str,
        screening_dir: str | Path = None,
        save: bool = True,
        progress_cb=None,
    ) -> str:
        """Run allocation analysis and return the report as a markdown string."""
        weights = load_weights()
        raw_contexts = self._build_contexts(results, screening_dir)
        # Inject weighted scores into every context
        contexts = [apply_weights(ctx, weights) for ctx in raw_contexts]

        # Load or distil the lessons library from past reflections
        lessons_block = load_cached_lessons(self.reflections_dir)
        if lessons_block is None:
            reflections = load_reflections(self.reflections_dir)
            if reflections:
                if progress_cb:
                    progress_cb(f"Distilling lessons from {len(reflections)} past reflections...")
                lessons_block = distill_lessons(
                    self.llm, reflections, reflections_dir=self.reflections_dir
                )
            else:
                lessons_block = ""

        if self.use_council:
            report = run_council(
                llm=self.llm,
                ticker_contexts=contexts,
                budget=self.budget,
                trade_date=trade_date,
                weights=weights,
                lessons_block=lessons_block,
                progress_cb=progress_cb,
                advisor_llms=self.advisor_llms,
            )
        else:
            analyst = create_allocation_analyst(self.llm, self.budget, trade_date)
            report = analyst(contexts)

        if save and screening_dir:
            out = Path(screening_dir) / "allocation.md"
            out.write_text(report, encoding="utf-8")

        return report

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_historical_scores(self, ticker: str, current_base: Path) -> list[dict]:
        """Return parsed score dicts from past screening runs for this ticker.

        Scans every sibling run directory (both screening_* and earnings_*,
        excluding the current one), reads the JSON block from earnings_brief.md,
        and returns them oldest-first.
        """
        from tradingagents.reports_layout import RUN_PREFIXES

        history: list[dict] = []
        reports_root = current_base.parent
        if not reports_root.is_dir():
            return history

        for d in sorted(reports_root.iterdir()):
            if not d.is_dir() or d == current_base:
                continue
            if not d.name.startswith(RUN_PREFIXES):
                continue
            brief_path = d / ticker / "earnings_brief.md"
            if not brief_path.exists():
                continue
            try:
                raw = brief_path.read_text(encoding="utf-8")
                m = re.search(r"### Scores\s*```json\s*(\{[^`]*?\})\s*```", raw, re.DOTALL)
                if not m:
                    continue
                scores = json.loads(m.group(1))
                date_m = re.match(r"(?:screening|earnings)_(\d{4}-\d{2}-\d{2})", d.name)
                scores["_date"] = date_m.group(1) if date_m else d.name
                history.append(scores)
            except Exception:
                continue

        return history

    def _build_contexts(
        self, results: list[dict], screening_dir: str | Path = None
    ) -> list[dict]:
        """Enrich each result dict with PM decision, brief summary, and screening history."""
        base = Path(screening_dir) if screening_dir else None
        contexts = []

        for r in results:
            ticker = r["ticker"]
            ctx = dict(r)
            if "sector" not in ctx:
                ctx["sector"] = "Unknown"

            if base:
                # Portfolio manager decision — full text up to 1500 chars
                pm_path = base / ticker / "5_portfolio" / "decision.md"
                if pm_path.exists():
                    ctx["pm_decision"] = _cut(pm_path.read_text(encoding="utf-8").strip(), 1500)
                else:
                    ctx["pm_decision"] = ctx.get("ta_decision", "Not available")

                # Earnings brief — strip score JSON block, keep up to 1200 chars
                brief_path = base / ticker / "earnings_brief.md"
                if brief_path.exists():
                    raw = brief_path.read_text(encoding="utf-8")
                    clean = re.sub(
                        r"### Scores\s*```json.*?```", "", raw, flags=re.DOTALL
                    ).strip()
                    ctx["brief_summary"] = _cut(clean, 1200)
                else:
                    ctx["brief_summary"] = ctx.get("one_liner", "Not available")

                # Fundamentals quality score — read from cache saved during screening;
                # fall back to LLM scoring if the cache file is missing.
                fund_score = None
                cached_path = base / ticker / "fundamentals_score.json"
                if cached_path.exists():
                    try:
                        fund_score = json.loads(cached_path.read_text(encoding="utf-8"))
                    except Exception:
                        pass

                if fund_score is None:
                    fundamentals_path = base / ticker / "1_analysts" / "fundamentals.md"
                    if fundamentals_path.exists():
                        try:
                            fundamentals_md = fundamentals_path.read_text(encoding="utf-8")
                            fund_score = score_fundamentals(
                                self.llm, fundamentals_md, ticker,
                                metrics=fetch_fundamental_metrics(ticker),
                            )
                            cached_path.write_text(
                                json.dumps(fund_score, indent=2), encoding="utf-8"
                            )
                        except Exception as exc:
                            logger.warning("%s: fundamentals scoring failed: %s", ticker, exc)

                if fund_score is None:
                    fund_score = {"fundamentals_score": 0, "balance_sheet": "Adequate",
                                  "profitability": "Stable", "growth_quality": "Medium",
                                  "summary": "No fundamentals data available."}

                ctx["fundamentals_score"]   = fund_score["fundamentals_score"]
                ctx["bs_quality"]           = fund_score.get("balance_sheet",  "Adequate")
                ctx["margin_trend"]         = fund_score.get("profitability",  "Stable")
                ctx["growth_quality"]       = fund_score.get("growth_quality", "Medium")
                ctx["fundamentals_summary"] = fund_score.get("summary", "")

                # Pricing context — cached at screening time; live fetch fallback
                pricing = None
                pricing_path = base / ticker / "pricing.json"
                if pricing_path.exists():
                    try:
                        pricing = json.loads(pricing_path.read_text(encoding="utf-8"))
                    except Exception:
                        pass
                if pricing is None:
                    try:
                        pricing = fetch_pricing_context(ticker, ctx.get("earnings_date"))
                        pricing_path.write_text(json.dumps(pricing, indent=2), encoding="utf-8")
                    except Exception:
                        pricing = None
                ctx["pricing_summary"] = format_pricing(pricing)

                # Payoff asymmetry / EV (#14a) — cached at screening time, live fallback
                asym = None
                asym_path = base / ticker / "asymmetry.json"
                if asym_path.exists():
                    try:
                        asym = json.loads(asym_path.read_text(encoding="utf-8"))
                    except Exception:
                        pass
                if asym is None:
                    try:
                        asym = build_asymmetry(
                            ticker,
                            beat_score=ctx.get("beat_score"),
                            implied_move_pct=(pricing or {}).get("implied_move_pct"),
                        )
                        asym_path.write_text(json.dumps(asym, indent=2), encoding="utf-8")
                    except Exception:
                        asym = None
                ctx["asymmetry"] = asym or {}
                ctx["asymmetry_summary"] = format_asymmetry(asym)

                # Crowding / run-up (#14c) — cached at screening time, live fallback
                crowding = None
                crowding_path = base / ticker / "crowding.json"
                if crowding_path.exists():
                    try:
                        crowding = json.loads(crowding_path.read_text(encoding="utf-8"))
                    except Exception:
                        pass
                if crowding is None:
                    try:
                        crowding = fetch_crowding(ticker, sector=ctx.get("sector"))
                        crowding_path.write_text(json.dumps(crowding, indent=2), encoding="utf-8")
                    except Exception:
                        crowding = None
                ctx["crowding"] = crowding or {}
                ctx["crowding_summary"] = format_crowding(crowding)

                # Peer read-through (#9) — cached at screening time, live fallback
                peers = None
                peers_path = base / ticker / "peers.json"
                if peers_path.exists():
                    try:
                        peers = json.loads(peers_path.read_text(encoding="utf-8"))
                    except Exception:
                        pass
                if peers is None:
                    try:
                        peers = build_peer_readthrough(ticker)
                        peers_path.write_text(json.dumps(peers, indent=2, default=str), encoding="utf-8")
                    except Exception:
                        peers = None
                ctx["peers"] = peers or {}
                ctx["peer_summary"] = format_peer_oneliner(peers)

            else:
                ctx.setdefault("fundamentals_score",   0)
                ctx.setdefault("bs_quality",           "Adequate")
                ctx.setdefault("margin_trend",         "Stable")
                ctx.setdefault("growth_quality",       "Medium")
                ctx.setdefault("fundamentals_summary", "")
                ctx.setdefault("pricing_summary",      "Not available")
                ctx.setdefault("asymmetry",            {})
                ctx.setdefault("asymmetry_summary",    "Not available")
                ctx.setdefault("crowding",             {})
                ctx.setdefault("crowding_summary",     "Not available")
                ctx.setdefault("peers",                {})
                ctx.setdefault("peer_summary",         "Not available")

            # Historical screening data
            history = self._load_historical_scores(ticker, base) if base else []
            if history:
                ctx["historical_count"] = len(history)
                ctx["historical_avg_total"] = round(
                    sum(h.get("total_score", 0) for h in history) / len(history), 1
                )
                recent = history[-3:]
                ctx["historical_brief"] = "  ·  ".join(
                    f"{h['_date']}: beat={h.get('beat_score',0):+d} "
                    f"guid={h.get('guidance_score',0):+d} "
                    f"setup={h.get('setup_score',0):+d} "
                    f"total={h.get('total_score',0):+d} → {h.get('signal','?')}"
                    for h in recent
                )
                if len(history) >= 2:
                    last_t = history[-1].get("total_score", 0)
                    prev_t = history[-2].get("total_score", 0)
                    if last_t > prev_t + 1:
                        ctx["score_trend"] = "Improving"
                    elif last_t < prev_t - 1:
                        ctx["score_trend"] = "Deteriorating"
                    else:
                        ctx["score_trend"] = "Stable"
                else:
                    ctx["score_trend"] = "First repeat"
            else:
                ctx["historical_count"] = 0
                ctx["historical_avg_total"] = None
                ctx["score_trend"] = "New ticker"
                ctx["historical_brief"] = "No prior screenings found."

            contexts.append(ctx)

        return contexts


def build_advisor_llms(config: dict) -> list:
    """Build per-advisor chat models from config["council_advisor_models"].

    Each entry is "provider:model" (e.g. "anthropic:claude-fable-5"). Entries
    that fail to construct are skipped with a warning. Returns an empty list
    when the key is unset, so the council falls back to its single main LLM.
    """
    from tradingagents.llm_clients import create_llm_client

    llms = []
    for entry in config.get("council_advisor_models") or []:
        provider, sep, model = str(entry).partition(":")
        if not sep or not provider.strip() or not model.strip():
            logger.warning("Ignoring malformed council_advisor_models entry: %r", entry)
            continue
        try:
            client = create_llm_client(provider=provider.strip(), model=model.strip())
            llms.append(client.get_llm())
        except Exception as exc:
            logger.warning("Could not build advisor model %r: %s", entry, exc)
    return llms


