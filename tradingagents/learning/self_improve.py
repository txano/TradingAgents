"""Automated self-improvement: analyse trade reflections, then apply changes.

Pipeline (driven by the `learn` CLI command):
  1. scan_reflection_items()  — gather every post-mortem + its trade outcome
  2. analyze_reflections()    — one deep-think LLM call returns a markdown report
                                plus a structured JSON proposal (weight changes +
                                anchored prompt-source edits + process notes)
  3. apply_weight_changes()   — deterministic, reversible (single JSON file)
  4. apply_prompt_edits()     — edits agent-prompt source files, but ONLY with a
                                strict safety harness (see apply_prompt_edits)

The prompt-edit harness is what makes "fully automated prompt edits" safe:
allowlisted files only, exact unique anchor match, f-string placeholder
preservation, per-file backup, and a post-edit `compile()` check that restores
the backup on any syntax break. Every change is also written to a changelog.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from tradingagents.allocation.weights import DEFAULTS as _WEIGHT_DEFAULTS
from tradingagents.allocation.weights import load_weights, save_weights
from tradingagents.reflection.layer import parse_reflection_score

logger = logging.getLogger(__name__)

# Only these files may be auto-edited. Paths are relative to the repo root.
# Anything outside this allowlist is rejected, so the LLM can never touch
# arbitrary code — only the prompt-bearing agent files.
EDITABLE_PROMPT_FILES: tuple[str, ...] = (
    "tradingagents/agents/analysts/market_analyst.py",
    "tradingagents/agents/analysts/fundamentals_analyst.py",
    "tradingagents/agents/analysts/news_analyst.py",
    "tradingagents/agents/analysts/social_media_analyst.py",
    "tradingagents/agents/analysts/sentiment_analyst.py",
    "tradingagents/agents/researchers/bull_researcher.py",
    "tradingagents/agents/researchers/bear_researcher.py",
    "tradingagents/agents/managers/research_manager.py",
    "tradingagents/agents/managers/portfolio_manager.py",
    "tradingagents/agents/risk_mgmt/aggressive_debator.py",
    "tradingagents/agents/risk_mgmt/conservative_debator.py",
    "tradingagents/agents/risk_mgmt/neutral_debator.py",
    "tradingagents/allocation/council.py",
    "tradingagents/allocation/fundamentals_scorer.py",
    "tradingagents/earnings/analyst.py",
)

_WEIGHT_MIN, _WEIGHT_MAX = 0.0, 3.0


# ---------------------------------------------------------------------------
# 1. Gather reflections
# ---------------------------------------------------------------------------

def scan_reflection_items(reports_dir: Path, all_trades: list[dict]) -> list[dict]:
    """One entry per unique ticker+exit_date (most-recent reflection wins)."""
    reflections_dir = reports_dir / "reflections"
    if not reflections_dir.exists():
        return []

    trade_lookup: dict = {}
    for t in all_trades:
        key = (t.get("ticker", ""), t.get("exit_date", ""))
        prev = trade_lookup.get(key)
        if prev is None or (t.get("reflected_at", "") or "") > (prev.get("reflected_at", "") or ""):
            trade_lookup[key] = t

    folders: list = []
    for d in reflections_dir.iterdir():
        if not d.is_dir() or not (d / "post_mortem.md").exists():
            continue
        parts = d.name.split("_")
        if len(parts) < 3:
            continue
        folders.append(("_".join(parts[2:]), d, parts[0], parts[1]))
    folders.sort(key=lambda x: x[0], reverse=True)

    seen: set = set()
    items: list = []
    for _ts, d, ticker, exit_date in folders:
        key = (ticker, exit_date)
        if key in seen:
            continue
        seen.add(key)
        content = (d / "post_mortem.md").read_text(encoding="utf-8")
        score = parse_reflection_score(content)
        trade = trade_lookup.get(key, {})
        items.append({
            "ticker":        ticker,
            "exit_date":     exit_date,
            "direction":     score.get("direction") or trade.get("direction", "?"),
            "pnl":           trade.get("pnl"),
            "pnl_pct":       score.get("pnl_pct") if score.get("pnl_pct") is not None else trade.get("pnl_pct"),
            "outcome":       score.get("outcome") or trade.get("outcome", "?"),
            "beat_correct":  score.get("beat_prediction_correct"),
            "guide_correct": score.get("guidance_prediction_correct"),
            "key_lesson":    score.get("key_lesson", ""),
            "content":       content,
        })
    items.sort(key=lambda x: x["exit_date"], reverse=True)
    return items


# ---------------------------------------------------------------------------
# 2. Analyse
# ---------------------------------------------------------------------------

_SYSTEM = (
    "You are a systematic trading-pipeline improvement expert. You analyse trade "
    "post-mortems and propose concrete, minimal, safe improvements to the pipeline's "
    "prompts and scoring weights. You ALWAYS ground recommendations in specific trades. "
    "When you propose a prompt edit you provide an EXACT substring of the current source "
    "as `old_string` (copied verbatim, unique within its file) and its replacement as "
    "`new_string`, preserving every `{placeholder}` token unchanged."
)


def _stats_block(items: list[dict]) -> str:
    n = len(items)
    wins = sum(1 for i in items if i["outcome"] == "WIN")
    losses = sum(1 for i in items if i["outcome"] == "LOSS")
    beat_all = [i for i in items if i["beat_correct"] is not None]
    guide_all = [i for i in items if i["guide_correct"] is not None]
    beat_acc = f"{sum(1 for i in beat_all if i['beat_correct'])}/{len(beat_all)}" if beat_all else "N/A"
    guide_acc = f"{sum(1 for i in guide_all if i['guide_correct'])}/{len(guide_all)}" if guide_all else "N/A"
    pnl_pcts = [i["pnl_pct"] for i in items if i["pnl_pct"] is not None]
    avg_pct = sum(pnl_pcts) / len(pnl_pcts) if pnl_pcts else 0.0
    win_rate = f"{wins / n * 100:.0f}%" if n else "N/A"
    return (
        f"| Reflections | {n} |\n"
        f"| Win / Loss | {wins} / {losses} ({win_rate} win rate) |\n"
        f"| Avg P&L % | {avg_pct:+.1f}% |\n"
        f"| Beat prediction accuracy | {beat_acc} |\n"
        f"| Guidance prediction accuracy | {guide_acc} |"
    )


def _read_editable_sources(repo_root: Path) -> dict[str, str]:
    sources: dict[str, str] = {}
    for rel in EDITABLE_PROMPT_FILES:
        p = repo_root / rel
        if p.exists():
            sources[rel] = p.read_text(encoding="utf-8")
    return sources


def build_improvement_prompt(items: list[dict], sources: dict[str, str]) -> str:
    cur = load_weights()
    lines = [
        "# Trade Reflection Analysis — Automated System Improvement",
        "",
        "## What TradingAgents does",
        "- 5 LangGraph teams: market / fundamentals / news / social analysts → bull/bear",
        "  researchers → research manager → risk mgmt → portfolio manager (BUY/SHORT/SKIP).",
        "- Four pre-earnings scores (−5..+5): `beat_score`, `guidance_score`, `setup_score`,",
        "  and `fundamentals_score` (business quality, grounded in real statement metrics).",
        "- `weighted_score = beat_w×beat + guidance_w×guidance + setup_w×setup + fundamentals_w×fundamentals`.",
        f"  Current weights: {json.dumps(cur)} (defaults {json.dumps(_WEIGHT_DEFAULTS)}).",
        "- An AI Council (5 persona advisors → cross-review → synthesis) sizes positions; a",
        "  deterministic validator enforces position/sector caps and budget arithmetic.",
        "",
        "## Batch statistics",
        "| Metric | Value |",
        "|--------|-------|",
        _stats_block(items),
        "",
        "## Post-mortems",
        "",
    ]
    for idx, item in enumerate(items, 1):
        lines.append(f"### {idx}. {item['ticker']} — {item['exit_date']} "
                     f"({item['direction']}, {item['outcome']})")
        lines.append(item["content"].strip())
        lines.append("\n---\n")

    lines += [
        "## Current editable prompt source",
        "You may propose edits ONLY to the files below. `old_string` must be copied",
        "verbatim from this source and be unique within its file.",
        "",
    ]
    for rel, src in sources.items():
        lines.append(f"<<<FILE {rel}>>>")
        lines.append("```python")
        lines.append(src)
        lines.append("```")
        lines.append("")

    lines += [
        "## Output format",
        "First write a concise markdown improvement report (failure patterns, success",
        "patterns, blind spots — cite specific tickers). Then, as the LAST thing in your",
        "reply, emit exactly one fenced ```json block with this shape:",
        "",
        "```json",
        "{",
        '  "weights": {"beat": <float|null>, "guidance": <float|null>, "setup": <float|null>, "fundamentals": <float|null>},',
        '  "weights_rationale": "<why, citing the beat/guidance accuracy stats and trades>",',
        '  "prompt_edits": [',
        '    {"file": "<path from the allowlist>", "old_string": "<verbatim unique snippet>",',
        '     "new_string": "<replacement, same {placeholders}>", "rationale": "<which trades motivate this>"}',
        "  ],",
        '  "process_notes": ["<structural ideas that are NOT prompt/weight edits>"]',
        "}",
        "```",
        "",
        "Rules: propose a weight only if the evidence supports it (else null). Keep prompt",
        "edits minimal and additive where possible. Never alter `{placeholder}` tokens. If",
        "you have no high-confidence change for a category, return an empty list / nulls.",
    ]
    return "\n".join(lines)


def _extract_proposal(text: str) -> dict:
    """Parse the last ```json block; return {} on failure."""
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    for block in reversed(blocks):
        try:
            return json.loads(block)
        except (json.JSONDecodeError, ValueError):
            continue
    return {}


def analyze_reflections(llm, items: list[dict], repo_root: Path) -> tuple[str, dict]:
    """Run the improvement analysis. Returns (report_markdown, proposal_dict)."""
    sources = _read_editable_sources(repo_root)
    prompt = build_improvement_prompt(items, sources)
    response = llm.invoke([("system", _SYSTEM), ("human", prompt)])
    report = response.content if hasattr(response, "content") else str(response)
    return report, _extract_proposal(report)


# ---------------------------------------------------------------------------
# 3. Apply weights (safe / deterministic)
# ---------------------------------------------------------------------------

def apply_weight_changes(proposal: dict, *, dry_run: bool = False) -> list[str]:
    """Apply proposed scoring-weight changes, clamped to [0, 3]. Returns changelog."""
    weights = proposal.get("weights") or {}
    current = load_weights()
    new = dict(current)
    changes: list[str] = []
    for key in _WEIGHT_DEFAULTS:
        val = weights.get(key)
        if val is None:
            continue
        try:
            clamped = max(_WEIGHT_MIN, min(_WEIGHT_MAX, float(val)))
        except (TypeError, ValueError):
            continue
        if abs(clamped - current.get(key, 0.0)) > 1e-9:
            new[key] = clamped
            changes.append(f"weight {key}: {current.get(key)} → {clamped}")

    if changes and not dry_run:
        save_weights(new["beat"], new["guidance"], new["setup"], new["fundamentals"])
    return changes


# ---------------------------------------------------------------------------
# 4. Apply prompt edits (guarded)
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\{[^{}]+\}")


def _placeholders_preserved(old: str, new: str) -> bool:
    """The set of {field} tokens and the brace balance must be unchanged."""
    if sorted(_PLACEHOLDER_RE.findall(old)) != sorted(_PLACEHOLDER_RE.findall(new)):
        return False
    return old.count("{") == new.count("{") and old.count("}") == new.count("}")


def apply_prompt_edits(
    edits: list[dict],
    repo_root: Path,
    backup_dir: Path,
    *,
    dry_run: bool = False,
) -> tuple[list[str], list[str]]:
    """Apply anchored prompt-source edits under a strict safety harness.

    For each edit, in order, ALL of these must hold or the edit is rejected
    (never partially applied): the target is on the allowlist and resolves
    inside the repo; `old_string` occurs exactly once; `{placeholder}` tokens
    and brace balance are preserved; and after writing, the file still
    `compile()`s. Any compile failure restores the pre-edit backup.

    Returns (applied, rejected) — both lists of human-readable strings.
    """
    applied: list[str] = []
    rejected: list[str] = []
    allow = set(EDITABLE_PROMPT_FILES)
    repo_root = repo_root.resolve()

    for i, edit in enumerate(edits or []):
        rel = str(edit.get("file", "")).strip()
        old = edit.get("old_string", "")
        new = edit.get("new_string", "")
        tag = f"{rel or '?'} (edit {i + 1})"

        if rel not in allow:
            rejected.append(f"{tag}: file not on the editable allowlist")
            continue
        path = (repo_root / rel).resolve()
        if repo_root not in path.parents or not path.exists():
            rejected.append(f"{tag}: resolves outside the repo or missing")
            continue
        if not old or old == new:
            rejected.append(f"{tag}: empty or no-op old_string")
            continue

        text = path.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            rejected.append(f"{tag}: old_string not found (source drifted?)")
            continue
        if count > 1:
            rejected.append(f"{tag}: old_string matches {count}× (not unique)")
            continue
        if not _placeholders_preserved(old, new):
            rejected.append(f"{tag}: would change f-string {{placeholders}} — rejected")
            continue

        updated = text.replace(old, new, 1)
        try:
            compile(updated, str(path), "exec")
        except SyntaxError as exc:
            rejected.append(f"{tag}: edit breaks Python syntax ({exc.msg}) — rejected")
            continue

        if dry_run:
            applied.append(f"{tag}: WOULD APPLY — {edit.get('rationale', '').strip()[:120]}")
            continue

        # Back up the original before touching it, then write.
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / rel.replace("/", "__")
        backup_path.write_text(text, encoding="utf-8")
        path.write_text(updated, encoding="utf-8")

        # Belt and braces: re-verify on disk; restore if anything is off.
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
            applied.append(f"{tag}: applied — {edit.get('rationale', '').strip()[:120]}")
        except SyntaxError as exc:
            path.write_text(text, encoding="utf-8")
            rejected.append(f"{tag}: post-write compile failed ({exc.msg}) — restored backup")

    return applied, rejected


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def apply_proposal(
    proposal: dict,
    repo_root: Path,
    run_dir: Path,
    *,
    n_reflections: int = 0,
    apply_weights: bool = True,
    apply_prompts: bool = True,
    dry_run: bool = False,
    progress_cb=None,
) -> dict:
    """Apply a proposal (weights + prompt edits) and write a changelog.

    Used both by `run_self_improvement` (fresh analysis) and when applying a
    previously reviewed `proposal.json` verbatim — no LLM call involved here.
    """
    def _log(msg: str) -> None:
        if progress_cb:
            progress_cb(msg)

    run_dir.mkdir(parents=True, exist_ok=True)

    weight_changes: list[str] = []
    if apply_weights:
        weight_changes = apply_weight_changes(proposal, dry_run=dry_run)
        _log(f"Weights: {len(weight_changes)} change(s).")

    applied: list[str] = []
    rejected: list[str] = []
    if apply_prompts:
        applied, rejected = apply_prompt_edits(
            proposal.get("prompt_edits", []), repo_root,
            run_dir / "backups", dry_run=dry_run,
        )
        _log(f"Prompt edits: {len(applied)} applied, {len(rejected)} rejected.")

    process_notes = proposal.get("process_notes", []) or []

    changelog = _render_changelog(
        n_reflections, weight_changes, applied, rejected, process_notes, dry_run
    )
    (run_dir / "CHANGELOG.md").write_text(changelog, encoding="utf-8")

    return {
        "report_path":    None,
        "changelog_path": run_dir / "CHANGELOG.md",
        "weight_changes": weight_changes,
        "prompt_applied": applied,
        "prompt_rejected": rejected,
        "process_notes":  process_notes,
        "dry_run":        dry_run,
    }


def run_self_improvement(
    llm,
    items: list[dict],
    repo_root: Path,
    run_dir: Path,
    *,
    apply_weights: bool = True,
    apply_prompts: bool = True,
    dry_run: bool = False,
    progress_cb=None,
) -> dict:
    """Analyse reflections and apply changes. Writes report + changelog to run_dir.

    Returns a summary dict: report_path, changelog_path, weight_changes,
    prompt_applied, prompt_rejected, process_notes.
    """
    def _log(msg: str) -> None:
        if progress_cb:
            progress_cb(msg)

    run_dir.mkdir(parents=True, exist_ok=True)
    _log("Analysing reflections...")
    report, proposal = analyze_reflections(llm, items, repo_root)
    (run_dir / "improvement_report.md").write_text(report, encoding="utf-8")
    (run_dir / "proposal.json").write_text(json.dumps(proposal, indent=2), encoding="utf-8")

    summary = apply_proposal(
        proposal, repo_root, run_dir,
        n_reflections=len(items),
        apply_weights=apply_weights, apply_prompts=apply_prompts,
        dry_run=dry_run, progress_cb=progress_cb,
    )
    summary["report_path"] = run_dir / "improvement_report.md"
    return summary


def _render_changelog(n_reflections, weight_changes, applied, rejected, process_notes, dry_run) -> str:
    head = "# Self-Improvement Changelog"
    if dry_run:
        head += " (DRY RUN — nothing was applied)"
    lines = [
        head,
        f"\n_{datetime.now().isoformat(timespec='seconds')} · {n_reflections} reflections analysed_\n",
        "## Scoring weights",
        ("\n".join(f"- {c}" for c in weight_changes) if weight_changes else "- no changes"),
        "\n## Prompt edits applied",
        ("\n".join(f"- {c}" for c in applied) if applied else "- none"),
        "\n## Prompt edits rejected (safety harness)",
        ("\n".join(f"- {c}" for c in rejected) if rejected else "- none"),
        "\n## Process notes (not auto-applied)",
        ("\n".join(f"- {n}" for n in process_notes) if process_notes else "- none"),
        "\n---\n_Revert anything via git; original files are also backed up in this run's `backups/`._",
    ]
    return "\n".join(lines)
