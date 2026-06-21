"""Lessons library: distil post-mortem reflections into actionable trading rules.

Usage:
    from tradingagents.learning.lessons import load_reflections, distill_lessons, load_cached_lessons

    reflections = load_reflections("reports/reflections")
    lessons_block = load_cached_lessons("reports/reflections") or distill_lessons(llm, reflections)
"""

import hashlib
import json
import logging
from pathlib import Path

from tradingagents.reflection.layer import parse_reflection_score

logger = logging.getLogger(__name__)

_CACHE_PATH = Path.home() / ".tradingagents" / "lessons_cache.md"
_CACHE_META_PATH = Path.home() / ".tradingagents" / "lessons_cache_meta.json"

_DISTILL_SYSTEM = """\
You are a senior quantitative analyst reviewing post-trade reflections from a \
pre-earnings trading strategy. Your job is to synthesize the lessons across all \
reviewed trades into a concise, actionable rules library that a portfolio manager \
can apply immediately when allocating capital.

Focus on:
- What score patterns (beat/guidance/setup combinations) reliably predicted outcomes
- What the market consistently punished even when scores looked good
- When to size up vs. stay small vs. skip entirely
- Cross-trade patterns that appear more than once

Be ruthlessly specific. Avoid generic investment advice. A rule must be \
supported by at least TWO trades showing the same pattern — one trade is an \
anecdote, not a rule. Patterns seen only once belong in "Patterns to Watch" \
as tentative observations, clearly labelled as single-trade evidence.
"""

_DISTILL_HUMAN = """\
Below are {n} post-trade reflections from real trades. Each entry includes the \
outcome (WIN/LOSS/BREAKEVEN), direction (BUY/SHORT), P&L %, and the analyst's \
key lesson from that trade.

=== TRADE REFLECTIONS ===
{reflection_text}

---
Synthesize these into a "Lessons Library" of concrete rules (as many as the \
evidence genuinely supports — fewer well-supported rules beat many weak ones). \
Format each rule as:

**Rule N: <short title>** (n=<number of supporting trades>)
<2–3 sentences explaining the pattern, when it applies, and what to do.>
Evidence: <comma-separated list of tickers this was observed on>

Only include rules with n ≥ 2. End with a brief "Patterns to Watch" paragraph \
(3–4 sentences) for tentative single-trade observations and recurring failure \
modes or opportunities the strategy should exploit going forward, each marked \
with its evidence count.
"""


def load_reflections(reflections_dir: str | Path = "reports/reflections") -> list[dict]:
    """Scan reflections_dir for post_mortem.md files and return parsed reflection data.

    Each returned dict has: ticker, direction, outcome, pnl_pct, key_lesson,
    prediction_accuracy, full_text.
    """
    base = Path(reflections_dir)
    if not base.exists():
        return []

    results = []
    for post_mortem_path in sorted(base.rglob("post_mortem.md")):
        try:
            text = post_mortem_path.read_text(encoding="utf-8")
            score = parse_reflection_score(text)
            if not score:
                continue
            results.append({
                "ticker":              score.get("ticker", "?"),
                "direction":           score.get("direction", "?"),
                "outcome":             score.get("outcome", "?"),
                "pnl_pct":             score.get("pnl_pct"),
                "key_lesson":          score.get("key_lesson", ""),
                "prediction_accuracy": score.get("prediction_accuracy", "?"),
                "full_text":           text,
            })
        except Exception as exc:
            logger.warning("Failed to parse %s: %s", post_mortem_path, exc)

    return results


def _reflections_digest(reflections_dir: str | Path) -> str:
    """Fingerprint of all post_mortem.md files (path, mtime, size).

    Unlike a plain file count, this invalidates the cache when a reflection is
    edited or replaced, not only when one is added or removed.
    """
    base = Path(reflections_dir)
    entries = []
    if base.exists():
        for p in sorted(base.rglob("post_mortem.md")):
            st = p.stat()
            entries.append(f"{p.relative_to(base)}|{st.st_mtime_ns}|{st.st_size}")
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


def distill_lessons(
    llm,
    reflection_data: list[dict],
    max_reflections: int = 30,
    reflections_dir: str | Path = "reports/reflections",
) -> str:
    """Call the LLM to distil reflection_data into an actionable lessons library.

    Returns a markdown string ready for injection into the council synthesis prompt.
    Caches the result to ~/.tradingagents/lessons_cache.md.
    """
    if not reflection_data:
        return "_No reflections available yet. Run more trades and use `tradingagents reflect` after each exit._"

    subset = reflection_data[:max_reflections]

    lines = []
    for r in subset:
        pnl = f"{r['pnl_pct']:+.1f}%" if r["pnl_pct"] is not None else "?"
        lines.append(
            f"[{r['ticker']} | {r['direction']} | {r['outcome']} | P&L {pnl} | "
            f"Accuracy: {r['prediction_accuracy']}]\n"
            f"Key lesson: {r['key_lesson']}\n"
        )

    reflection_text = "\n".join(lines)
    human = _DISTILL_HUMAN.format(n=len(subset), reflection_text=reflection_text)

    try:
        response = llm.invoke([("system", _DISTILL_SYSTEM), ("human", human)])
        lessons_block = response.content
    except Exception as exc:
        logger.warning("Lessons distillation LLM call failed: %s", exc)
        lessons_block = "_Lessons distillation failed. Check LLM configuration._"

    _save_cache(lessons_block, _reflections_digest(reflections_dir))
    return lessons_block


def load_cached_lessons(reflections_dir: str | Path = "reports/reflections") -> str | None:
    """Return cached lessons markdown if the cache is still valid, else None.

    Cache is valid when the digest of post_mortem.md files (paths, mtimes,
    sizes) has not changed since the last distillation run.
    """
    if not _CACHE_PATH.exists() or not _CACHE_META_PATH.exists():
        return None

    try:
        meta = json.loads(_CACHE_META_PATH.read_text(encoding="utf-8"))
        if meta.get("reflections_digest") == _reflections_digest(reflections_dir):
            return _CACHE_PATH.read_text(encoding="utf-8")
    except Exception:
        pass

    return None


def _save_cache(lessons_block: str, reflections_digest: str) -> None:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(lessons_block, encoding="utf-8")
    _CACHE_META_PATH.write_text(
        json.dumps({"reflections_digest": reflections_digest}, indent=2),
        encoding="utf-8",
    )
