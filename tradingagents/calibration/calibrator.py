"""Calibrate screening predictions against actual earnings outcomes."""

import json
import math
import re
from datetime import datetime, timedelta
from pathlib import Path

try:
    import yfinance as yf
except ImportError:
    yf = None


def _safe_float(val):
    """Convert to float, return None if NaN/None/error."""
    if val is None:
        return None
    try:
        f = float(val)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def parse_screening_table(table_path: Path) -> list[dict]:
    """Parse screening_table.md and return rows as list of dicts.

    Handles both the old format (no Sector column) and the current format
    that includes Sector between Ticker and Earnings.
    """
    text = table_path.read_text(encoding="utf-8")
    has_sector = False
    rows = []

    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        if re.match(r"^\|[-| ]+\|$", line):
            continue
        parts = [p.strip() for p in line.split("|")]
        parts = [p for p in parts if p]
        if not parts:
            continue
        # Detect header row and whether Sector column is present
        if parts[0] == "#":
            has_sector = "Sector" in parts
            continue
        if len(parts) < 9:
            continue
        try:
            if has_sector:
                # | # | Ticker | Sector | Earnings | Beat | Guidance | Setup | Total | Signal | Confidence | One-liner |
                rows.append({
                    "rank": parts[0],
                    "ticker": parts[1],
                    "sector": parts[2],
                    "earnings_date": parts[3],
                    "beat_score": int(parts[4]),
                    "guidance_score": int(parts[5]),
                    "setup_score": int(parts[6]),
                    "total_score": int(parts[7]),
                    "signal": parts[8],
                    "confidence": parts[9],
                    "one_liner": parts[10] if len(parts) > 10 else "",
                })
            else:
                # | # | Ticker | Earnings | Beat | Guidance | Setup | Total | Signal | Confidence | One-liner |
                rows.append({
                    "rank": parts[0],
                    "ticker": parts[1],
                    "sector": "Unknown",
                    "earnings_date": parts[2],
                    "beat_score": int(parts[3]),
                    "guidance_score": int(parts[4]),
                    "setup_score": int(parts[5]),
                    "total_score": int(parts[6]),
                    "signal": parts[7],
                    "confidence": parts[8],
                    "one_liner": parts[9] if len(parts) > 9 else "",
                })
        except (ValueError, IndexError):
            continue
    return rows


def fetch_actual_result(ticker: str, expected_date: str, lookback_days: int = 7) -> dict:
    """Fetch actual EPS and price action around earnings from yfinance.

    Returns dict with keys: reported_eps, estimated_eps, beat, surprise_pct,
    price_before, price_after, price_change_pct.
    """
    result = {
        "reported_eps": None,
        "estimated_eps": None,
        "beat": None,
        "surprise_pct": None,
        "price_before": None,
        "price_after": None,
        "price_change_pct": None,
    }

    if yf is None:
        return result

    try:
        t = yf.Ticker(ticker)
        target = datetime.strptime(expected_date, "%Y-%m-%d").date()

        # Fetch EPS actuals from earnings_dates
        try:
            earnings_df = t.earnings_dates
            if earnings_df is not None and not earnings_df.empty:
                for idx, row in earnings_df.iterrows():
                    row_date = idx.date() if hasattr(idx, "date") else idx
                    if abs((row_date - target).days) <= lookback_days:
                        result["reported_eps"] = _safe_float(row.get("Reported EPS"))
                        result["estimated_eps"] = _safe_float(row.get("EPS Estimate"))
                        result["surprise_pct"] = _safe_float(row.get("Surprise(%)"))
                        if result["reported_eps"] is not None and result["estimated_eps"] is not None:
                            result["beat"] = result["reported_eps"] >= result["estimated_eps"]
                        elif result["surprise_pct"] is not None:
                            result["beat"] = result["surprise_pct"] >= 0
                        break
        except Exception:
            pass

        # Fetch price action around earnings date
        try:
            start = (datetime.strptime(expected_date, "%Y-%m-%d") - timedelta(days=5)).strftime("%Y-%m-%d")
            end = (datetime.strptime(expected_date, "%Y-%m-%d") + timedelta(days=5)).strftime("%Y-%m-%d")
            hist = t.history(start=start, end=end)
            if not hist.empty:
                dates = [d.date() if hasattr(d, "date") else d for d in hist.index]
                before = [d for d in dates if d < target]
                after = [d for d in dates if d >= target]
                if before:
                    result["price_before"] = _safe_float(hist["Close"].iloc[dates.index(before[-1])])
                if len(after) > 1:
                    result["price_after"] = _safe_float(hist["Close"].iloc[dates.index(after[1])])
                elif after:
                    result["price_after"] = _safe_float(hist["Close"].iloc[dates.index(after[0])])
                if result["price_before"] and result["price_after"]:
                    result["price_change_pct"] = (
                        (result["price_after"] - result["price_before"]) / result["price_before"] * 100
                    )
        except Exception:
            pass

    except Exception:
        pass

    return result


def calibrate_screening_run(screening_dir: Path) -> dict:
    """Calibrate a single screening run against actual earnings outcomes.

    Parses screening_table.md, fetches actuals from yfinance, computes accuracy,
    and writes calibration.json + calibration.md to the same directory.
    """
    table_path = screening_dir / "screening_table.md"
    if not table_path.exists():
        raise FileNotFoundError(f"No screening_table.md found in {screening_dir}")

    rows = parse_screening_table(table_path)
    if not rows:
        raise ValueError(f"No parseable rows in {table_path}")

    calibration = []
    for row in rows:
        actual = fetch_actual_result(row["ticker"], row["earnings_date"])

        beat_prediction = row["beat_score"] > 0
        beat_correct = None
        if actual.get("beat") is not None:
            beat_correct = beat_prediction == actual["beat"]

        signal_correct = None
        if actual.get("price_change_pct") is not None:
            price_went_up = actual["price_change_pct"] > 0
            if row["signal"] == "BUY":
                signal_correct = price_went_up
            elif row["signal"] == "SHORT":
                signal_correct = not price_went_up
            # SKIP: signal_correct stays None (N/A)

        calibration.append({
            "ticker": row["ticker"],
            "earnings_date": row["earnings_date"],
            "beat_score": row["beat_score"],
            "guidance_score": row["guidance_score"],
            "setup_score": row["setup_score"],
            "total_score": row["total_score"],
            "signal": row["signal"],
            "confidence": row["confidence"],
            "reported_eps": actual.get("reported_eps"),
            "estimated_eps": actual.get("estimated_eps"),
            "actual_beat": actual.get("beat"),
            "surprise_pct": actual.get("surprise_pct"),
            "price_change_pct": actual.get("price_change_pct"),
            "beat_prediction_correct": beat_correct,
            "signal_correct": signal_correct,
        })

    # Compute summary stats
    with_beat = [c for c in calibration if c["beat_prediction_correct"] is not None]
    with_signal = [c for c in calibration if c["signal_correct"] is not None]

    beat_accuracy = (
        sum(1 for c in with_beat if c["beat_prediction_correct"]) / len(with_beat) * 100
        if with_beat else None
    )
    signal_accuracy = (
        sum(1 for c in with_signal if c["signal_correct"]) / len(with_signal) * 100
        if with_signal else None
    )

    result = {
        "screening_dir": str(screening_dir),
        "calibrated_at": datetime.now().isoformat(),
        "tickers": len(calibration),
        "beat_accuracy_pct": round(beat_accuracy, 1) if beat_accuracy is not None else None,
        "signal_accuracy_pct": round(signal_accuracy, 1) if signal_accuracy is not None else None,
        "rows": calibration,
    }

    (screening_dir / "calibration.json").write_text(json.dumps(result, indent=2), encoding="utf-8")

    # Build markdown report
    ba_str = f"{beat_accuracy:.1f}%" if beat_accuracy is not None else "N/A (insufficient data)"
    sa_str = f"{signal_accuracy:.1f}%" if signal_accuracy is not None else "N/A (insufficient data)"

    lines = [
        f"# Calibration — {screening_dir.name}\n\n",
        f"Calibrated: {result['calibrated_at'][:10]}\n\n",
        "## Summary\n\n",
        f"- **Beat prediction accuracy:** {ba_str}  ({len(with_beat)}/{len(calibration)} tickers with data)\n",
        f"- **Signal accuracy:** {sa_str}  ({len(with_signal)}/{len(calibration)} tickers with data)\n\n",
        "## Results\n\n",
        "| Ticker | Earnings | Beat | Guidance | Setup | Total | Conf | Signal | Actual | Surprise% | Price Δ% | Beat✓ | Signal✓ |\n",
        "|--------|----------|------|----------|-------|-------|------|--------|--------|-----------|----------|-------|----------|\n",
    ]
    for c in calibration:
        b_sym = "✓" if c["beat_prediction_correct"] else ("✗" if c["beat_prediction_correct"] is False else "?")
        s_sym = "✓" if c["signal_correct"] else ("✗" if c["signal_correct"] is False else "N/A")
        act = "Beat" if c["actual_beat"] else ("Miss" if c["actual_beat"] is False else "?")
        surp = f"{c['surprise_pct']:+.1f}%" if c["surprise_pct"] is not None else "?"
        pc = f"{c['price_change_pct']:+.1f}%" if c["price_change_pct"] is not None else "?"
        lines.append(
            f"| {c['ticker']} | {c['earnings_date']} "
            f"| {c['beat_score']:+d} | {c['guidance_score']:+d} | {c['setup_score']:+d} | {c['total_score']:+d} "
            f"| {c['confidence']} | {c['signal']} "
            f"| {act} | {surp} | {pc} | {b_sym} | {s_sym} |\n"
        )

    (screening_dir / "calibration.md").write_text("".join(lines), encoding="utf-8")

    # Always refresh the master calibration file in the parent reports/ folder
    update_master_calibration(screening_dir.parent)

    return result


def update_master_calibration(reports_dir: Path) -> None:
    """Rebuild calibration_master.json and calibration_master.md from all runs.

    Called automatically at the end of every calibrate_screening_run call.
    """
    runs = load_all_calibrations(reports_dir)
    if not runs:
        return

    all_rows = [row for run in runs for row in run.get("rows", [])]

    with_beat = [r for r in all_rows if r["beat_prediction_correct"] is not None]
    with_signal = [r for r in all_rows if r["signal_correct"] is not None]

    overall_beat_acc = (
        sum(1 for r in with_beat if r["beat_prediction_correct"]) / len(with_beat) * 100
        if with_beat else None
    )
    overall_signal_acc = (
        sum(1 for r in with_signal if r["signal_correct"]) / len(with_signal) * 100
        if with_signal else None
    )

    master = {
        "last_updated": datetime.now().isoformat(),
        "total_runs": len(runs),
        "total_tickers": len(all_rows),
        "overall_beat_accuracy_pct": round(overall_beat_acc, 1) if overall_beat_acc is not None else None,
        "overall_signal_accuracy_pct": round(overall_signal_acc, 1) if overall_signal_acc is not None else None,
        "runs": runs,
    }

    (reports_dir / "calibration_master.json").write_text(
        json.dumps(master, indent=2), encoding="utf-8"
    )

    # Markdown
    ba_str = f"{overall_beat_acc:.1f}%" if overall_beat_acc is not None else "N/A"
    sa_str = f"{overall_signal_acc:.1f}%" if overall_signal_acc is not None else "N/A"

    md_lines = [
        "# Calibration Master\n\n",
        f"Last updated: {master['last_updated'][:10]}\n\n",
        "## Overall Summary\n\n",
        f"- **Runs calibrated:** {len(runs)}\n",
        f"- **Total tickers:** {len(all_rows)}\n",
        f"- **Beat prediction accuracy:** {ba_str}  ({len(with_beat)} tickers with data)\n",
        f"- **Signal accuracy:** {sa_str}  ({len(with_signal)} tickers with data)\n\n",
        "## Per-Run Summary\n\n",
        "| Run | Tickers | Beat Acc. | Signal Acc. |\n",
        "|-----|---------|-----------|-------------|\n",
    ]
    for run in runs:
        run_name = Path(run["screening_dir"]).name
        ba = f"{run['beat_accuracy_pct']}%" if run.get("beat_accuracy_pct") is not None else "?"
        sa = f"{run['signal_accuracy_pct']}%" if run.get("signal_accuracy_pct") is not None else "?"
        md_lines.append(f"| {run_name} | {run['tickers']} | {ba} | {sa} |\n")

    md_lines += [
        "\n## All Results\n\n",
        "| Run | Ticker | Earnings | Beat | Guidance | Setup | Total | Conf | Signal | Actual | Surprise% | Price Δ% | Beat✓ | Signal✓ |\n",
        "|-----|--------|----------|------|----------|-------|-------|------|--------|--------|-----------|----------|-------|----------|\n",
    ]
    for run in runs:
        run_name = Path(run["screening_dir"]).name
        for c in run.get("rows", []):
            bpc = c.get("beat_prediction_correct")
            sc_val = c.get("signal_correct")
            b_sym = "✓" if bpc else ("✗" if bpc is False else "?")
            s_sym = "✓" if sc_val else ("✗" if sc_val is False else "N/A")
            act_beat = c.get("actual_beat")
            act = "Beat" if act_beat else ("Miss" if act_beat is False else "?")
            surp_val = c.get("surprise_pct")
            pc_val = c.get("price_change_pct")
            surp = f"{surp_val:+.1f}%" if surp_val is not None else "?"
            pc = f"{pc_val:+.1f}%" if pc_val is not None else "?"
            md_lines.append(
                f"| {run_name} | {c.get('ticker', '?')} | {c.get('earnings_date', '?')} "
                f"| {c.get('beat_score', 0):+d} | {c.get('guidance_score', 0):+d} "
                f"| {c.get('setup_score', 0):+d} | {c.get('total_score', 0):+d} "
                f"| {c.get('confidence', '?')} | {c.get('signal', '?')} "
                f"| {act} | {surp} | {pc} | {b_sym} | {s_sym} |\n"
            )

    (reports_dir / "calibration_master.md").write_text("".join(md_lines), encoding="utf-8")


def load_all_calibrations(reports_dir: Path) -> list[dict]:
    """Load all calibration.json files from screening runs, newest first."""
    results = []
    for cal_file in sorted(reports_dir.glob("screening_*/calibration.json"), reverse=True):
        try:
            results.append(json.loads(cal_file.read_text(encoding="utf-8")))
        except Exception:
            continue
    return results


def list_uncalibrated_runs(reports_dir: Path) -> list[Path]:
    """Return screening dirs that have a screening_table.md but no calibration.json."""
    out = []
    for d in sorted(reports_dir.glob("screening_*/")):
        if d.is_dir() and (d / "screening_table.md").exists() and not (d / "calibration.json").exists():
            out.append(d)
    return out
