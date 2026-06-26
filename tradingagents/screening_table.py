"""The screening_table.md format — single source of truth for the writer.

The ranked screening table is written by the CLI `screen`/`allocate` commands and
the dashboard server, and parsed back by `calibration.calibrator.parse_screening_table`.
Keeping one writer here stops those call sites from drifting on the column order.

NB: the column layout below must stay in sync with `parse_screening_table`.
"""

from __future__ import annotations

from pathlib import Path

_HEADER_ROW = (
    "| # | Ticker | Sector | Earnings | Beat | Guidance | Setup | Total | Signal | Confidence | One-liner |\n"
)
_SEPARATOR_ROW = (
    "|---|--------|--------|----------|------|----------|-------|-------|--------|------------|-----------|\n"
)


def render_screening_table(results: list[dict], header: str) -> str:
    """Render the ranked screening table as markdown.

    `results` is ranked by total_score (descending) here, so the `#` column is
    always correct regardless of the order the caller passes them in. `header` is
    the leading H1 line (e.g. "# Earnings Screener — 2026-06-24").
    """
    ranked = sorted(results, key=lambda r: r.get("total_score", 0), reverse=True)
    lines = [f"{header}\n\n", _HEADER_ROW, _SEPARATOR_ROW]
    for i, r in enumerate(ranked, 1):
        lines.append(
            f"| {i} | {r['ticker']} | {r.get('sector', 'Unknown')} | {r.get('earnings_date', '?')} "
            f"| {r.get('beat_score', 0):+d} | {r.get('guidance_score', 0):+d} "
            f"| {r.get('setup_score', 0):+d} | {r.get('total_score', 0):+d} "
            f"| {r.get('signal', '?')} | {r.get('confidence', '?')} "
            f"| {r.get('one_liner', '')} |\n"
        )
    return "".join(lines)


def write_screening_table(results: list[dict], path: str | Path, header: str) -> None:
    """Render and write screening_table.md to `path`."""
    Path(path).write_text(render_screening_table(results, header), encoding="utf-8")
