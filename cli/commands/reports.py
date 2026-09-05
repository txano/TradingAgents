"""Static reports site builder — _build_reports_data, build_web, _auto_build_web."""

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

console = Console()


def _load_json(path: Path):
    """Best-effort JSON load; returns None on missing/invalid."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _extract_brief_scores(brief_md: str) -> dict:
    """Parse the JSON score block at the bottom of an earnings_brief.md."""
    import re
    import json
    m = re.search(r'```json\s*(\{.*?\})\s*```', brief_md, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except Exception:
        return {}


def _build_reports_data(
    reports_dir: Path,
    trades_path: Path,
    *,
    limit_runs: int | None = None,
    run_offset: int = 0,
    include_bodies: bool = True,
    limit_trades: int | None = None,
    limit_reflections: int | None = None,
    refresh_watchlist: bool = True,
) -> dict:
    """Collect report data into a single dict for the dashboard / static site.

    Full fidelity by default — that is what `build-web` needs, since a static
    page has no server to fetch anything from later.

    The live dashboard passes limits instead. Report *bodies*
    (`earnings_brief_md`, `portfolio_decision_md`) dominate the payload — ~20 MB
    of a 27 MB page across 3,000 ticker rows — yet are only ever read when a
    single ticker is expanded, so the server sends them on demand via
    `/api/report` and sets `include_bodies=False` here. Skipping them also
    avoids thousands of file reads, which is most of the build time.
    """
    import json
    import re
    import datetime as dt

    trades: list = []
    if trades_path.exists():
        try:
            trades = json.loads(trades_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    total_trades = len(trades)
    all_trades = trades                     # every stat below is computed over this
    if limit_trades is not None:
        # Trade log is oldest-first, so the most recent are at the tail.
        # Guard limit_trades == 0: trades[-0:] is the whole list, not none of it.
        trades = trades[-limit_trades:] if limit_trades > 0 else []

    from tradingagents.reports_layout import iter_run_dirs

    screening_runs: list = []
    all_run_dirs = iter_run_dirs(reports_dir)
    total_runs = len(all_run_dirs)
    if limit_runs is not None:
        all_run_dirs = all_run_dirs[run_offset:run_offset + limit_runs]
    elif run_offset:
        all_run_dirs = all_run_dirs[run_offset:]

    for d in all_run_dirs:
        if not d.is_dir():
            continue
        name = d.name
        parts = name.split("_")
        date_str = parts[1] if len(parts) > 1 else ""

        table_md = alloc_md = None
        table_path = d / "screening_table.md"
        alloc_path = d / "allocation.md"
        if table_path.exists():
            raw = table_path.read_text(encoding="utf-8")
            table_md = raw[:80_000] if len(raw) > 80_000 else raw
        if alloc_path.exists():
            raw = alloc_path.read_text(encoding="utf-8")
            alloc_md = raw[:60_000] if len(raw) > 60_000 else raw

        alloc_by_ticker: dict = {}
        if alloc_md:
            try:
                m_alloc = re.search(r'```json\s*(\{.*?\})\s*```', alloc_md, re.DOTALL)
                if m_alloc:
                    alloc_json = json.loads(m_alloc.group(1))
                    for entry in alloc_json.get("allocations", []):
                        tk = entry.get("ticker", "")
                        if tk:
                            alloc_by_ticker[tk] = {
                                "amount":     entry.get("amount", 0),
                                "pct":        entry.get("pct_of_budget", 0.0),
                                "direction":  entry.get("direction"),
                                "conviction": entry.get("conviction"),
                                "rationale":  entry.get("rationale"),
                            }
            except Exception:
                pass

        metadata: dict = {}
        meta_path = d / "metadata.json"
        if meta_path.exists():
            try:
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        cal_summary = None
        cal_path = d / "calibration.json"
        if cal_path.exists():
            try:
                cal_data = json.loads(cal_path.read_text(encoding="utf-8"))
                rows = cal_data.get("rows", [])
                if rows:
                    n_cal   = len(rows)
                    sig_ok  = sum(1 for r in rows if r.get("signal_correct") is True)
                    beat_ok = sum(1 for r in rows if r.get("beat_prediction_correct") is True)
                    beat_n  = sum(1 for r in rows if r.get("beat_prediction_correct") is not None)
                    cal_summary = {
                        "signal_accuracy_pct": sig_ok / n_cal * 100 if n_cal else None,
                        "beat_accuracy_pct":   beat_ok / beat_n * 100 if beat_n else None,
                        "n": n_cal,
                    }
            except Exception:
                pass

        tickers: list = []
        for td in sorted(d.iterdir()):
            if not td.is_dir():
                continue
            brief_path = td / "earnings_brief.md"
            if not brief_path.exists():
                continue
            brief_raw = brief_path.read_text(encoding="utf-8")
            scores    = _extract_brief_scores(brief_raw)
            pm_path   = td / "5_portfolio" / "decision.md"
            capped = pm_md = None
            if include_bodies:
                capped = brief_raw[:15_000] if len(brief_raw) > 15_000 else brief_raw
                if pm_path.exists():
                    raw_pm = pm_path.read_text(encoding="utf-8")
                    pm_md  = raw_pm[:12_000] if len(raw_pm) > 12_000 else raw_pm
            tk_alloc = alloc_by_ticker.get(td.name, {})

            fund_score = _load_json(td / "fundamentals_score.json") or {}

            # Council signal artifacts (#14 pricing/EV/asymmetry/crowding, #9 peers).
            # Drop the heavy per-print "reactions" array from asymmetry to keep the
            # embedded payload small.
            pricing  = _load_json(td / "pricing.json")
            asym     = _load_json(td / "asymmetry.json")
            crowding = _load_json(td / "crowding.json")
            peers    = _load_json(td / "peers.json")
            if isinstance(asym, dict):
                asym = {k: v for k, v in asym.items() if k != "reactions"}

            tickers.append({
                "ticker":                td.name,
                "signal":                scores.get("signal"),
                "confidence":            scores.get("confidence"),
                "beat_score":            scores.get("beat_score"),
                "guidance_score":        scores.get("guidance_score"),
                "setup_score":           scores.get("setup_score"),
                "total_score":           scores.get("total_score"),
                "one_liner":             scores.get("one_liner"),
                "fundamentals_score":    fund_score.get("fundamentals_score"),
                "bs_quality":            fund_score.get("balance_sheet"),
                "margin_trend":          fund_score.get("profitability"),
                "growth_quality":        fund_score.get("growth_quality"),
                "fundamentals_summary":  fund_score.get("summary"),
                "allocation_amount":     tk_alloc.get("amount"),
                "allocation_pct":        tk_alloc.get("pct"),
                "allocation_direction":  tk_alloc.get("direction"),
                "allocation_conviction": tk_alloc.get("conviction"),
                "allocation_rationale":  tk_alloc.get("rationale"),
                "pricing":               pricing,
                "asymmetry":             asym,
                "crowding":              crowding,
                "peers":                 peers,
                "earnings_brief_md":     capped,
                "portfolio_decision_md": pm_md,
                # Bodies may be omitted (see include_bodies); these say whether one
                # exists to fetch, so the UI can tell "not loaded" from "not there".
                "has_brief":             True,
                "has_decision":          pm_path.exists(),
            })

        screening_runs.append({
            "id":                 name,
            "date":               date_str,
            "earnings_date":      metadata.get("earnings_date") or (date_str if name.startswith("earnings_") else None),
            "depth":              metadata.get("depth"),
            "run_type":           metadata.get("run_type", "screening" if name.startswith("screening_") else "earnings"),
            "provider":           metadata.get("provider"),
            "n_tickers":          len(tickers),
            "screening_table_md": table_md,
            "allocation_md":      alloc_md,
            "calibration":        cal_summary,
            "tickers":            tickers,
        })

    standalone: list = []
    analysis_base = reports_dir / "analysis"
    analysis_dirs = sorted(analysis_base.iterdir(), reverse=True) if analysis_base.is_dir() else []
    for d in analysis_dirs:
        if not d.is_dir():
            continue
        brief_path    = d / "earnings_brief.md"
        complete_path = d / "complete_report.md"
        report_path   = brief_path if brief_path.exists() else (complete_path if complete_path.exists() else None)
        if not report_path:
            continue
        brief_raw = report_path.read_text(encoding="utf-8")
        parts2    = d.name.split("_")
        ticker2   = parts2[0] if parts2 else d.name
        raw_date  = parts2[1] if len(parts2) > 1 else ""
        date2     = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}" if len(raw_date) >= 8 else ""
        capped    = brief_raw[:15_000] if len(brief_raw) > 15_000 else brief_raw
        pm_path   = d / "5_portfolio" / "decision.md"
        pm_md     = pm_path.read_text(encoding="utf-8")[:12_000] if pm_path.exists() else None
        # Parse the score block so standalone runs are comparable to screened
        # ones on the Analyses page (a complete_report.md may have no score
        # block at all — missing keys just come through as None).
        scores2   = _extract_brief_scores(brief_raw)
        standalone.append({
            "id": d.name, "ticker": ticker2, "date": date2,
            "signal":      scores2.get("signal"),
            "confidence":  scores2.get("confidence"),
            "total_score": scores2.get("total_score"),
            "one_liner":   scores2.get("one_liner"),
            "earnings_brief_md": capped, "portfolio_decision_md": pm_md,
            "report_type": "brief" if brief_path.exists() else "analysis",
        })

    reflections: list = []
    reflections_dir = reports_dir / "reflections"
    if reflections_dir.exists():
        seen_refl: set = set()
        for d in sorted(reflections_dir.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            pm_path = d / "post_mortem.md"
            if not pm_path.exists():
                continue
            parts3 = d.name.split("_")
            if len(parts3) < 2:
                continue
            ticker3    = parts3[0]
            exit_date3 = parts3[1] if len(parts3) > 1 else ""
            key3 = (ticker3, exit_date3)
            if key3 in seen_refl:
                continue
            seen_refl.add(key3)
            pm_raw  = pm_path.read_text(encoding="utf-8")
            score3: dict = {}
            try:
                from tradingagents.reflection.layer import parse_reflection_score as _prs
                score3 = _prs(pm_raw) or {}
            except Exception:
                score3 = _extract_brief_scores(pm_raw)
            reflections.append({
                "id":            d.name,
                "ticker":        ticker3,
                "exit_date":     exit_date3,
                "outcome":       score3.get("outcome"),
                "key_lesson":    score3.get("key_lesson", ""),
                "post_mortem_md": pm_raw[:30_000] if len(pm_raw) > 30_000 else pm_raw,
            })

    total_reflections = len(reflections)
    if limit_reflections is not None:
        reflections = reflections[:limit_reflections]

    # Deliberately `all_trades`, not the truncated `trades`: these are
    # whole-history figures. Computing them from the display slice made the
    # Overview report $28k of lifetime P&L instead of $143k.
    stats: dict = {}
    if all_trades:
        n_t      = len(all_trades)
        wins_t   = sum(1 for t in all_trades if (t.get("pnl") or 0) > 0)
        losses_t = sum(1 for t in all_trades if (t.get("pnl") or 0) < 0)
        total_pnl = sum(t.get("pnl") or 0 for t in all_trades)
        stats["wins"]     = wins_t
        stats["losses"]   = losses_t
        stats["total_pnl"] = total_pnl
        stats["win_rate"] = wins_t / n_t * 100 if n_t else 0

        _pos: dict = {}
        for t in all_trades:
            key = (t.get("ticker", ""), t.get("exit_date", ""))
            sh  = t.get("shares") or 0
            ep  = t.get("entry_price") or 0
            if key not in _pos:
                _pos[key] = {"exit_date": key[1], "_sh": sh, "_ep_w": ep * sh, "_pnl": t.get("pnl") or 0}
            else:
                g = _pos[key]
                g["_sh"]   += sh
                g["_ep_w"] += ep * sh
                g["_pnl"]  += t.get("pnl") or 0

        _daily: dict = {}
        for g in _pos.values():
            d2   = g["exit_date"]
            if not d2:
                continue
            sh2  = g["_sh"]
            ep2  = g["_ep_w"] / sh2 if sh2 > 0 else 0
            cost = ep2 * sh2
            if d2 not in _daily:
                _daily[d2] = {"capital": 0.0, "pnl": 0.0}
            _daily[d2]["capital"] += cost
            _daily[d2]["pnl"]     += g["_pnl"]

        for dd in _daily.values():
            dd["ret_pct"] = dd["pnl"] / dd["capital"] * 100 if dd["capital"] > 0 else 0.0

        dates_sorted  = sorted(_daily.keys())
        n_days        = len(dates_sorted)
        first_date    = dates_sorted[0] if dates_sorted else None
        avg_daily_cap = sum(dd["capital"] for dd in _daily.values()) / n_days if n_days else 0

        stats["daily_capital"]    = _daily
        stats["n_trading_days"]   = n_days
        stats["first_date"]       = first_date
        stats["last_date"]        = dates_sorted[-1] if dates_sorted else None
        stats["avg_daily_capital"] = avg_daily_cap
        stats["return_pct"]        = total_pnl / avg_daily_cap * 100 if avg_daily_cap > 0 else None
        stats["today"]             = dt.date.today().isoformat()

        if first_date and avg_daily_cap > 0:
            bench: dict = {}
            try:
                import yfinance as _yf2
                for sym in ("QQQ", "SPY"):
                    try:
                        df = _yf2.download(sym, start=first_date, end=stats["today"], progress=False, auto_adjust=True)
                        if df.empty:
                            continue
                        close = df["Close"].squeeze() if hasattr(df["Close"], "squeeze") else df["Close"]
                        p0 = float(close.iloc[0])
                        p1 = float(close.iloc[-1])
                        ret = (p1 - p0) / p0
                        bench[sym] = {
                            "price_start": round(p0, 2),
                            "price_end":   round(p1, 2),
                            "ret_pct":     round(ret * 100, 4),
                            "pnl":         avg_daily_cap * ret,
                        }
                    except Exception:
                        pass
            except ImportError:
                pass
            stats["benchmark"] = bench

    # Watchlist (#19) — WATCH entries across runs with live trigger status.
    # The live-quote refresh is a network round-trip per ticker (~2s), which is
    # most of the dashboard's page-build time. The server passes
    # refresh_watchlist=False and lets the page's own /api/watchlist call fill in
    # live prices a moment later; the static build keeps them baked in, having no
    # server to ask afterwards.
    try:
        from tradingagents.allocation.watchlist import collect_watchlists
        watchlist = collect_watchlists(reports_dir, refresh=refresh_watchlist)
    except Exception:
        watchlist = []

    return {
        "generated_at":       dt.datetime.now().isoformat(),
        "trades":              trades,
        "screening_runs":      screening_runs,
        # Paging metadata: what the caller got vs. what exists on disk.
        "total_runs":          total_runs,
        "run_offset":          run_offset,
        "total_trades":        total_trades,
        "total_reflections":   total_reflections,
        "bodies_included":     include_bodies,
        "standalone_analyses": standalone,
        "reflections":         reflections,
        "watchlist":           watchlist,
        "stats":               stats,
    }


def _write_reports_site(reports_dir: Path, trades_path: Path) -> Path:
    """Build reports/web/index.html with all report data embedded."""
    import json

    template_path = Path(__file__).parent.parent / "static" / "reports_site.html"
    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")

    data    = _build_reports_data(reports_dir, trades_path)
    template = template_path.read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False, default=str)
    html    = template.replace("__TRADINGAGENTS_DATA__", payload, 1)

    out_dir  = reports_dir / "web"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "index.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path


def _auto_build_web() -> None:
    """Silently rebuild reports/web/index.html after any data-generating command."""
    try:
        rp = Path("reports")
        if not rp.exists():
            return
        from cli.commands.common import _trades_path
        tp = _trades_path()
        _write_reports_site(rp, tp)
    except Exception:
        pass


def build_web(
    # no parameters — kept as plain function for app.command() registration
):
    """Build a static reports website at reports/web/index.html.

    Embeds all report data (trades, screenings, reflections, calibration)
    into a single HTML file you can open in any browser — no server needed.
    """
    reports_dir = Path("reports")
    from cli.commands.common import _trades_path
    trades_path = _trades_path()

    if not reports_dir.exists():
        console.print("[yellow]No reports/ directory found. Run a screen first.[/yellow]")
        raise typer.Exit(1)

    console.print("[dim]Building reports site…[/dim]")
    with console.status("[dim]Collecting data and fetching benchmark prices…[/dim]"):
        try:
            out_path = _write_reports_site(reports_dir, trades_path)
        except Exception as e:
            console.print(f"[red]Error building site: {e}[/red]")
            raise typer.Exit(1)

    console.print(f"[green]✓ Built:[/green] {out_path.resolve()}")
    console.print("[dim]Open that file in any browser — no server needed.[/dim]")


def build_screening_index(reports_dir: Path, since: str = "", tickers: "set | None" = None) -> dict:
    """Compact ticker -> screenings map for the earnings calendar.

    The calendar cross-references each upcoming company against past screenings.
    Deriving that from the dashboard's *paged* run list made it depend on how
    far the user had scrolled: a run that had not been loaded simply did not
    exist as far as the calendar was concerned, so its day looked unscreened.

    This walks the run dirs directly and returns only what a calendar row shows
    — scores and a one-liner, no report bodies — so it stays a few hundred KB
    across every run rather than tens of MB. ``since`` (YYYY-MM-DD) limits the
    scan to runs whose label date is on or after it.
    """
    from tradingagents.reports_layout import iter_run_dirs, run_sort_key

    index: dict[str, list] = {}
    for d in iter_run_dirs(reports_dir):
        label = run_sort_key(d)[0]
        if since and label and label < since:
            continue
        meta = _load_json(d / "metadata.json") or {}

        alloc_by_ticker: dict = {}
        alloc_path = d / "allocation.md"
        if alloc_path.exists():
            try:
                import re as _re
                m = _re.search(r'```json\s*(\{.*?\})\s*```',
                               alloc_path.read_text(encoding="utf-8"), _re.DOTALL)
                if m:
                    for entry in json.loads(m.group(1)).get("allocations", []):
                        if entry.get("ticker"):
                            alloc_by_ticker[entry["ticker"]] = entry.get("amount")
            except Exception:
                pass

        for td in sorted(d.iterdir()):
            if not td.is_dir() or not (td / "earnings_brief.md").exists():
                continue
            if tickers is not None and td.name not in tickers:
                continue
            scores = _extract_brief_scores((td / "earnings_brief.md").read_text(encoding="utf-8"))
            if not scores:
                continue
            fund = _load_json(td / "fundamentals_score.json") or {}
            index.setdefault(td.name, []).append({
                "run_id":             d.name,
                "date":               label,
                "earnings_date":      meta.get("earnings_date") or label,
                "depth":              meta.get("depth"),
                "signal":             scores.get("signal"),
                "confidence":         scores.get("confidence"),
                "beat_score":         scores.get("beat_score"),
                "guidance_score":     scores.get("guidance_score"),
                "setup_score":        scores.get("setup_score"),
                "total_score":        scores.get("total_score"),
                "fundamentals_score": fund.get("fundamentals_score"),
                "one_liner":          scores.get("one_liner"),
                "allocation_amount":  alloc_by_ticker.get(td.name),
            })
    for rows in index.values():
        rows.sort(key=lambda r: r.get("date") or "", reverse=True)
    return index
