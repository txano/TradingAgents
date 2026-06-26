"""Batch trade-reflection engine (non-interactive).

Consolidates fills into per-trade groups and runs the ReflectionLayer over them
without any prompts, so the whole trade history can be reflected on in one shot.
The interactive `reflect` CLI command and the automated `learn` command both
build on `consolidate_trades`; only `learn` uses `reflect_all`.
"""

from __future__ import annotations

import datetime
from collections import OrderedDict
from pathlib import Path

from tradingagents.reflection.layer import parse_reflection_score


def find_analysis_for_ticker(reports_dir: Path, ticker: str) -> list[Path]:
    """Return analysis folders for the given ticker, newest first."""
    candidates: list[Path] = []
    if not reports_dir.exists():
        return candidates
    from tradingagents.reports_layout import iter_run_dirs

    for folder in reports_dir.glob(f"{ticker}_*/"):
        if folder.is_dir() and (folder / "complete_report.md").exists():
            candidates.append(folder)
    for screening_dir in iter_run_dirs(reports_dir):
        ticker_dir = screening_dir / ticker
        if ticker_dir.is_dir():
            candidates.append(ticker_dir)
    return sorted(candidates, key=lambda p: p.name, reverse=True)


def find_existing_reflection(reports_dir: Path, ticker: str, exit_date: str) -> Path | None:
    """Most recent reflection folder matching TICKER_EXITDATE_*, or None."""
    reflections_dir = reports_dir / "reflections"
    if not reflections_dir.exists():
        return None
    prefix = f"{ticker}_{exit_date}_"
    matches = sorted(
        (d for d in reflections_dir.iterdir() if d.is_dir() and d.name.startswith(prefix)),
        reverse=True,
    )
    return matches[0] if matches else None


def consolidate_trades(indexed: list[tuple[int, dict]], reports_dir: Path) -> list[dict]:
    """Merge fills with the same ticker+exit_date into single consolidated groups.

    `indexed` is a list of (original_index, trade_dict). Each returned group
    carries share-weighted entry/exit prices, summed P&L, the original indices
    (so trades.json can be updated in place), and whether a reflection already
    exists for it.
    """
    trade_map = {orig_idx: t for orig_idx, t in indexed}
    groups: dict = OrderedDict()

    for orig_idx, t in indexed:
        key = (t.get("ticker", ""), t.get("exit_date", ""))
        if key not in groups:
            groups[key] = {
                "ticker":        t.get("ticker", ""),
                "exit_date":     t.get("exit_date", ""),
                "direction":     t.get("direction", "BUY"),
                "sector":        t.get("sector", ""),
                "pnl":           0.0,
                "shares":        0.0,
                "_entry_wtd":    0.0,
                "_exit_wtd":     0.0,
                "fills":         0,
                "orig_indices":  [],
                "analysis_path": None,
                "trade_date":    None,
                "key_lesson":    None,
            }
        g = groups[key]
        sh = t.get("shares") or 0
        g["pnl"]        += t.get("pnl") or 0
        g["shares"]     += sh
        g["_entry_wtd"] += (t.get("entry_price") or 0) * sh
        g["_exit_wtd"]  += (t.get("exit_price")  or 0) * sh
        g["fills"]      += 1
        g["orig_indices"].append(orig_idx)
        if not g["analysis_path"] and t.get("analysis_path"):
            g["analysis_path"] = t["analysis_path"]
        if not g["trade_date"] and t.get("trade_date"):
            g["trade_date"] = t["trade_date"]
        if not g["key_lesson"] and t.get("key_lesson"):
            g["key_lesson"] = t["key_lesson"]

    result: list[dict] = []
    for g in groups.values():
        sh          = g["shares"]
        entry_price = g["_entry_wtd"] / sh if sh else 0.0
        exit_price  = g["_exit_wtd"]  / sh if sh else 0.0
        cost        = entry_price * sh
        pnl_pct     = g["pnl"] / cost * 100 if cost else 0.0
        outcome     = "WIN" if g["pnl"] > 0.005 else "LOSS" if g["pnl"] < -0.005 else "BREAK_EVEN"

        existing_rp: Path | None = None
        for idx in g["orig_indices"]:
            rp = trade_map[idx].get("reflection_path")
            if rp and Path(rp).exists():
                existing_rp = Path(rp)
                break
        if existing_rp is None:
            existing_rp = find_existing_reflection(reports_dir, g["ticker"], g["exit_date"])

        del g["_entry_wtd"], g["_exit_wtd"]
        result.append({
            **g,
            "entry_price":              entry_price,
            "exit_price":               exit_price,
            "pnl_pct":                  pnl_pct,
            "outcome":                  outcome,
            "reflected":                existing_rp is not None,
            "existing_reflection_path": existing_rp,
        })
    return result


def resolve_analysis_path(group: dict, reports_dir: Path) -> Path | None:
    """Non-interactive analysis-folder resolution: stored path, else newest match."""
    stored = group.get("analysis_path")
    if stored and Path(stored).exists():
        return Path(stored)
    matches = find_analysis_for_ticker(reports_dir, group["ticker"])
    return matches[0] if matches else None


def reflect_all(
    layer,
    all_trades: list[dict],
    reports_dir: Path,
    *,
    force: bool = False,
    progress_cb=None,
) -> tuple[list[dict], list[dict]]:
    """Reflect on every consolidated trade, non-interactively.

    Args:
        layer: a ReflectionLayer instance.
        all_trades: parsed trades.json (mutated copy is returned).
        reports_dir: the reports/ directory.
        force: when False, groups that already have a reflection are skipped;
            when True, every group is re-reflected and overwrites.
        progress_cb: optional callable(message) for progress output.

    Returns:
        (updated_trades, results) where results is one dict per group with
        keys ticker, exit_date, status ("done"|"skipped"|"error"), reflection_path.
    """
    def _log(msg: str) -> None:
        if progress_cb:
            progress_cb(msg)

    trades = [dict(t) for t in all_trades]
    indexed = sorted(enumerate(trades), key=lambda x: x[1].get("exit_date", ""), reverse=True)
    groups = consolidate_trades(indexed, reports_dir)

    pending = groups if force else [g for g in groups if not g["reflected"]]
    results: list[dict] = []
    _log(f"{len(pending)} of {len(groups)} trade(s) to reflect on"
         f"{' (force: re-running all)' if force else ' (pending only)'}.")

    for n, group in enumerate(pending, 1):
        ticker     = group["ticker"]
        exit_date  = group["exit_date"]
        trade_date = group.get("trade_date") or exit_date
        analysis_path = resolve_analysis_path(group, reports_dir)

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        save_dir = reports_dir / "reflections" / f"{ticker}_{exit_date}_{timestamp}"

        _log(f"[{n}/{len(pending)}] Reflecting on {ticker} ({exit_date})...")
        try:
            post_mortem = layer.analyze(
                ticker=ticker,
                trade_date=trade_date,
                exit_date=exit_date,
                direction=group["direction"],
                shares=group["shares"],
                entry_price=group["entry_price"],
                exit_price=group["exit_price"],
                prior_analysis_path=str(analysis_path) if analysis_path else None,
                save_dir=str(save_dir),
            )
        except Exception as exc:  # noqa: BLE001 — never let one trade abort the batch
            _log(f"    error: {exc}")
            results.append({"ticker": ticker, "exit_date": exit_date,
                            "status": "error", "reflection_path": None})
            continue

        score = parse_reflection_score(post_mortem) if post_mortem else {}

        screening_run = None
        if analysis_path:
            from tradingagents.reports_layout import RUN_PREFIXES
            for parent in Path(analysis_path).parents:
                if parent.name.startswith(RUN_PREFIXES):
                    screening_run = str(parent)
                    break
        if not screening_run:
            for idx in group["orig_indices"]:
                sr = trades[idx].get("screening_run")
                if sr:
                    screening_run = sr
                    break

        update = {
            "beat_prediction_correct":     score.get("beat_prediction_correct"),
            "guidance_prediction_correct": score.get("guidance_prediction_correct"),
            "key_lesson":                  score.get("key_lesson", ""),
            "outcome":                     score.get("outcome") or group["outcome"],
            "prediction_accuracy":         score.get("prediction_accuracy", "UNKNOWN"),
            "screening_run":               screening_run,
            "analysis_path":               str(analysis_path) if analysis_path else group.get("analysis_path"),
            "reflection_path":             str(save_dir),
            "reflected_at":                datetime.datetime.now().isoformat(),
        }
        for idx in group["orig_indices"]:
            trades[idx].update(update)

        results.append({"ticker": ticker, "exit_date": exit_date,
                        "status": "done", "reflection_path": str(save_dir)})

    return trades, results
