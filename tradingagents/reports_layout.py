"""Canonical layout for batch screening run directories.

Screening runs live under ``reports/earnings/`` and are named either
``screening_*`` (a plain screen) or ``earnings_*`` (a calendar-driven earnings
screen). Some older runs may sit at the repo ``reports/`` root. This module is the
single place that knows where to find them, so the CLI commands, the dashboard
server, the calibrator, and the reflection loop can't drift on the layout.
"""

from __future__ import annotations

from pathlib import Path

# A batch screening run dir is named with one of these prefixes.
RUN_PREFIXES = ("screening_", "earnings_")


def runs_root(reports_dir: str | Path = "reports") -> Path:
    """Canonical parent dir that new screening runs are written to."""
    return Path(reports_dir) / "earnings"


def iter_run_dirs(reports_dir: str | Path = "reports") -> list[Path]:
    """All batch screening run dirs, newest first (by name).

    Looks under ``reports/earnings/`` (current layout) and the legacy repo root,
    matching both ``screening_*`` and ``earnings_*`` prefixes. ``reports_dir`` is
    always the repo reports root (e.g. ``Path("reports")``). De-duplicated.
    """
    base = Path(reports_dir)
    out: list[Path] = []

    earnings_base = base / "earnings"
    if earnings_base.is_dir():
        out += [d for d in earnings_base.iterdir()
                if d.is_dir() and d.name.startswith(RUN_PREFIXES)]

    # Legacy repo-root runs (pre-reports/earnings layout).
    out += [d for d in base.glob("screening_*") if d.is_dir()]

    seen: set = set()
    uniq: list[Path] = []
    for d in out:
        r = d.resolve()
        if r not in seen:
            seen.add(r)
            uniq.append(d)
    return sorted(uniq, key=lambda p: p.name, reverse=True)
