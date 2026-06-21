"""Static reports site builder — _build_reports_data, build_web, _auto_build_web."""

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

console = Console()


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


def _build_reports_data(reports_dir: Path, trades_path: Path) -> dict:
    """Collect all report data into a single dict for the static reports site."""
    import json
    import re
    import datetime as dt

    trades: list = []
    if trades_path.exists():
        try:
            trades = json.loads(trades_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    screening_runs: list = []
    earnings_base = reports_dir / "earnings"
    all_run_dirs = sorted(
        list(earnings_base.glob("screening_*/")) + list(earnings_base.glob("earnings_*/")),
        key=lambda p: p.name,
        reverse=True,
    ) if earnings_base.is_dir() else []

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
                                "amount": entry.get("amount", 0),
                                "pct":    entry.get("pct_of_budget", 0.0),
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
            capped    = brief_raw[:15_000] if len(brief_raw) > 15_000 else brief_raw
            pm_path   = td / "5_portfolio" / "decision.md"
            pm_md     = None
            if pm_path.exists():
                raw_pm = pm_path.read_text(encoding="utf-8")
                pm_md  = raw_pm[:12_000] if len(raw_pm) > 12_000 else raw_pm
            tk_alloc = alloc_by_ticker.get(td.name, {})

            fund_score: dict = {}
            fund_path = td / "fundamentals_score.json"
            if fund_path.exists():
                try:
                    fund_score = json.loads(fund_path.read_text(encoding="utf-8"))
                except Exception:
                    pass

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
                "earnings_brief_md":     capped,
                "portfolio_decision_md": pm_md,
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
        brief_path = d / "earnings_brief.md"
        if not brief_path.exists():
            continue
        brief_raw = brief_path.read_text(encoding="utf-8")
        parts2    = d.name.split("_")
        ticker2   = parts2[0] if parts2 else d.name
        raw_date  = parts2[1] if len(parts2) > 1 else ""
        date2     = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}" if len(raw_date) >= 8 else ""
        capped    = brief_raw[:15_000] if len(brief_raw) > 15_000 else brief_raw
        standalone.append({"id": d.name, "ticker": ticker2, "date": date2, "earnings_brief_md": capped})

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

    stats: dict = {}
    if trades:
        n_t      = len(trades)
        wins_t   = sum(1 for t in trades if (t.get("pnl") or 0) > 0)
        losses_t = sum(1 for t in trades if (t.get("pnl") or 0) < 0)
        total_pnl = sum(t.get("pnl") or 0 for t in trades)
        stats["wins"]     = wins_t
        stats["losses"]   = losses_t
        stats["total_pnl"] = total_pnl
        stats["win_rate"] = wins_t / n_t * 100 if n_t else 0

        _pos: dict = {}
        for t in trades:
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

    return {
        "generated_at":       dt.datetime.now().isoformat(),
        "trades":              trades,
        "screening_runs":      screening_runs,
        "standalone_analyses": standalone,
        "reflections":         reflections,
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
