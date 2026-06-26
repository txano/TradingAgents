"""TradingAgents web server — FastAPI backend for the reports dashboard and job runner."""

import asyncio
import datetime
import queue
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
load_dotenv(".env.enterprise", override=False)

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.earnings import EarningsLayer
from tradingagents.earnings.scorer import parse_score
from tradingagents.allocation.layer import parse_allocation
from tradingagents.screening_table import write_screening_table
from cli.commands.common import _fetch_sector, gather_api_keys
from cli.commands.screen import run_allocation, screen_ticker

app = FastAPI(title="TradingAgents", docs_url=None, redoc_url=None)

# --------------------------------------------------------------------------- #
# Job registry
# --------------------------------------------------------------------------- #

_jobs: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()

# --------------------------------------------------------------------------- #
# Calendar storage
# --------------------------------------------------------------------------- #

CALENDAR_PATH = Path.home() / ".tradingagents" / "calendar.json"
_CAL_LOCK = threading.Lock()


def _load_calendar() -> dict:
    with _CAL_LOCK:
        if CALENDAR_PATH.exists():
            import json as _json
            return _json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))
        return {}


def _save_calendar(data: dict) -> None:
    import json as _json
    with _CAL_LOCK:
        CALENDAR_PATH.parent.mkdir(parents=True, exist_ok=True)
        CALENDAR_PATH.write_text(_json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _next_market_day(date_str: str) -> str:
    d = datetime.date.fromisoformat(date_str)
    d += datetime.timedelta(days=1)
    while d.weekday() >= 5:  # skip Sat/Sun
        d += datetime.timedelta(days=1)
    return d.isoformat()


def _fetch_day_calendar_entries(trading_day_str: str) -> list:
    """AH entries for trading_day + PM entries for the next market day."""
    from cli.commands.calendar import _fetch_earnings_nasdaq
    ah_date = trading_day_str
    pm_date = _next_market_day(trading_day_str)
    entries = []
    for r in _fetch_earnings_nasdaq(ah_date):
        if r["time"] == "After hrs":
            entries.append({
                "ticker": r["ticker"], "company": r["company"],
                "time": r["time"], "session": "AH", "report_date": ah_date,
                "eps_estimate": r.get("eps_estimate"),
                "market_cap": r.get("market_cap"), "manual": False,
            })
    for r in _fetch_earnings_nasdaq(pm_date):
        if r["time"] == "Pre-mkt":
            entries.append({
                "ticker": r["ticker"], "company": r["company"],
                "time": r["time"], "session": "PM", "report_date": pm_date,
                "eps_estimate": r.get("eps_estimate"),
                "market_cap": r.get("market_cap"), "manual": False,
            })
    return entries


def _new_job(job_type: str, params: dict) -> str:
    job_id = str(uuid.uuid4())[:8]
    with _JOBS_LOCK:
        _jobs[job_id] = {
            "id": job_id,
            "type": job_type,
            "params": params,
            "status": "pending",
            "log": [],
            "queue": queue.Queue(),
        }
    return job_id


def _log(job_id: str, msg: str) -> None:
    with _JOBS_LOCK:
        job = _jobs.get(job_id)
    if job:
        job["log"].append(msg)
        job["queue"].put(msg)


def _finish(job_id: str, success: bool = True) -> None:
    sentinel = "__DONE__" if success else "__ERROR__"
    with _JOBS_LOCK:
        job = _jobs.get(job_id)
    if job:
        job["status"] = "done" if success else "error"
        job["queue"].put(sentinel)


# --------------------------------------------------------------------------- #
# Config helpers
# --------------------------------------------------------------------------- #

def _build_config(params: dict) -> tuple[dict, list[str]]:
    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = params.get("provider", "deepseek").lower()
    config["quick_think_llm"] = params.get("quick_model", "deepseek-chat")
    config["deep_think_llm"] = params.get("deep_model", "deepseek-chat")
    config["max_debate_rounds"] = int(params.get("depth", 1))
    config["max_risk_discuss_rounds"] = int(params.get("depth", 1))
    config["backend_url"] = params.get("backend_url") or None
    return config, gather_api_keys(config["llm_provider"])


# --------------------------------------------------------------------------- #
# Job runners — each runs in its own thread
# --------------------------------------------------------------------------- #

def _run_screen(job_id: str, params: dict) -> None:
    log = lambda msg: _log(job_id, msg)
    try:
        tickers = [t.strip().upper() for t in params.get("tickers", "").split(",") if t.strip()]
        if not tickers:
            log("ERROR: No tickers provided.")
            _finish(job_id, False)
            return

        trade_date    = params.get("date", datetime.date.today().isoformat())
        earnings_date = params.get("earnings_date", "").strip()
        analysts      = params.get("analysts", ["market", "social", "news", "fundamentals"])
        budget        = int(params.get("budget", 100_000))
        workers       = int(params.get("workers", 1))

        config, api_keys = _build_config(params)

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        if earnings_date:
            folder_name = f"earnings_{earnings_date}_{timestamp}"
        else:
            folder_name = f"screening_{trade_date}_{timestamp}"
        from tradingagents.reports_layout import runs_root
        screening_dir = runs_root() / folder_name
        screening_dir.mkdir(parents=True, exist_ok=True)

        import json as _json_meta
        _meta = {
            "run_type": "earnings" if earnings_date else "screening",
            "earnings_date": earnings_date or None,
            "trade_date": trade_date,
            "depth": int(params.get("depth", 1)),
            "provider": config.get("llm_provider", ""),
            "quick_model": config.get("quick_think_llm", ""),
            "deep_model": config.get("deep_think_llm", ""),
            "run_at": datetime.datetime.now().isoformat(),
        }
        (screening_dir / "metadata.json").write_text(_json_meta.dumps(_meta, indent=2), encoding="utf-8")

        log(f"Tickers ({len(tickers)}): {', '.join(tickers)}")
        log(f"Date: {trade_date}  |  Earnings date: {earnings_date or '—'}  |  Depth: {params.get('depth', 1)}  |  Workers: {workers}  |  Budget: ${budget:,}")
        log(f"Provider: {config['llm_provider']}  |  Models: {config['quick_think_llm']} / {config['deep_think_llm']}")
        log(f"Output: {screening_dir.name}/")
        log("")

        results: list[dict] = []
        results_lock = threading.Lock()

        def process(ticker: str, worker_config: dict) -> None:
            # Shared with the CLI `screen` command so both write the same artifacts
            # (pricing/asymmetry/crowding/peers.json) into the same layout.
            ticker_dir = screening_dir / ticker
            log(f"[{ticker}] Starting...")
            result = screen_ticker(
                ticker, trade_date, ticker_dir, worker_config,
                analysts=analysts, log=log,
            )
            if result.get("signal") == "ERROR":
                log(f"[{ticker}] ERROR: {result.get('one_liner', '')}")
            else:
                log(f"[{ticker}] Done → {result['signal']}  score: {result.get('total_score', 0):+d}")
            with results_lock:
                results.append(result)

        def _make_worker_config(idx: int) -> dict:
            wcfg = config.copy()
            if api_keys:
                wcfg["api_key"] = api_keys[idx % len(api_keys)]
            return wcfg

        if workers == 1:
            for i, ticker in enumerate(tickers):
                process(ticker, _make_worker_config(i))
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(process, t, _make_worker_config(i)): t
                        for i, t in enumerate(tickers)}
                for fut in as_completed(futs):
                    try:
                        fut.result()
                    except Exception:
                        pass

        # Save results table
        sorted_results = sorted(results, key=lambda r: r.get("total_score", 0), reverse=True)
        write_screening_table(
            sorted_results, screening_dir / "screening_table.md",
            f"# Earnings Screener — {trade_date}",
        )

        log("")
        log("── Results ────────────────────────────────────────────")
        for r in sorted_results:
            tot = r.get("total_score", 0)
            liner = r.get("one_liner", "")[:60]
            log(f"  {r['ticker']:<6}  {r.get('signal','?'):<6}  {tot:+d}  {liner}")

        # Allocation
        log("")
        log("Running Allocation Manager...")
        try:
            allocation_report = run_allocation(
                sorted_results, trade_date, screening_dir, budget, config,
                analysts=analysts, progress=lambda msg: log(f"  {msg}"),
            )
            if allocation_report:
                alloc_data = parse_allocation(allocation_report)
                deployed   = alloc_data.get("total_deployed", 0)
                log(f"Allocation done — deployed: ${deployed:,}")
        except Exception as exc:
            log(f"Allocation error: {exc}")

        log("")
        log(f"Saved to: {screening_dir.resolve()}")

        try:
            from cli.commands.reports import _auto_build_web
            _auto_build_web()
        except Exception:
            pass

        _finish(job_id, True)

    except Exception as exc:
        _log(job_id, f"FATAL: {exc}")
        _finish(job_id, False)


def _run_analyze(job_id: str, params: dict) -> None:
    log = lambda msg: _log(job_id, msg)
    try:
        ticker = params.get("ticker", "").strip().upper()
        if not ticker:
            log("ERROR: No ticker provided.")
            _finish(job_id, False)
            return

        trade_date = params.get("date", datetime.date.today().isoformat())
        analysts   = params.get("analysts", ["market", "social", "news", "fundamentals"])
        config, _  = _build_config(params)

        log(f"Analyzing {ticker} on {trade_date}")
        log(f"Analysts: {', '.join(analysts)}  |  Depth: {params.get('depth', 1)}")
        log(f"Provider: {config['llm_provider']}  |  Models: {config['quick_think_llm']} / {config['deep_think_llm']}")
        log("")

        ta = TradingAgentsGraph(analysts, debug=False, config=config)
        log(f"[{ticker}] Running agent graph...")
        final_state, decision = ta.propagate(ticker, trade_date)

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path("reports") / "analysis" / f"{ticker}_{timestamp}"
        out_dir.mkdir(parents=True, exist_ok=True)

        from cli.commands.common import save_report_to_disk
        save_report_to_disk(final_state, ticker, out_dir)

        log(f"[{ticker}] Decision: {decision}")
        log(f"Report saved to: {out_dir.name}/")

        try:
            from cli.commands.reports import _auto_build_web
            _auto_build_web()
        except Exception:
            pass

        _finish(job_id, True)

    except Exception as exc:
        _log(job_id, f"FATAL: {exc}")
        _finish(job_id, False)


def _run_calibrate(job_id: str, params: dict) -> None:
    log = lambda msg: _log(job_id, msg)
    try:
        from tradingagents.calibration import calibrate_screening_run

        run_name = params.get("run_name", "").strip()
        reports_dir = Path("reports")

        earnings_base = reports_dir / "earnings"
        if run_name:
            target = earnings_base / run_name
        else:
            # Pick the most recent uncalibrated run (both screening_/earnings_ prefixes)
            from tradingagents.reports_layout import iter_run_dirs
            candidates = [r for r in iter_run_dirs(reports_dir)
                          if (r / "screening_table.md").exists() and not (r / "calibration.json").exists()]
            if not candidates:
                log("No uncalibrated screening runs found.")
                _finish(job_id, False)
                return
            target = candidates[0]

        if not (target / "screening_table.md").exists():
            log(f"ERROR: {target.name} has no screening_table.md.")
            _finish(job_id, False)
            return

        log(f"Calibrating: {target.name}")
        log("Fetching actual earnings data from yfinance...")

        result = calibrate_screening_run(target)
        if result:
            log("")
            log(str(result)[:3000])

        log("")
        log("Calibration complete.")

        try:
            from cli.commands.reports import _auto_build_web
            _auto_build_web()
        except Exception:
            pass

        _finish(job_id, True)

    except Exception as exc:
        _log(job_id, f"FATAL: {exc}")
        _finish(job_id, False)


def _run_reflect(job_id: str, params: dict) -> None:
    log = lambda msg: _log(job_id, msg)
    try:
        import json as _json
        from tradingagents.reflection import ReflectionLayer

        ticker     = params.get("ticker", "").strip().upper()
        exit_date  = params.get("exit_date", "").strip()
        config, _  = _build_config(params)

        if not ticker or not exit_date:
            log("ERROR: ticker and exit_date are required.")
            _finish(job_id, False)
            return

        from cli.commands.common import _trades_path
        trade_log_path = _trades_path()
        if not trade_log_path.exists():
            log("ERROR: No trades.json found. Import trades first.")
            _finish(job_id, False)
            return

        all_trades = _json.loads(trade_log_path.read_text(encoding="utf-8"))
        matching = [t for t in all_trades
                    if t.get("ticker", "").upper() == ticker
                    and t.get("exit_date", "") == exit_date]
        if not matching:
            log(f"ERROR: No trade found for {ticker} on {exit_date}.")
            _finish(job_id, False)
            return

        trade = matching[0]
        log(f"Reflecting on {ticker} — exit {exit_date}  P&L: {trade.get('pnl', '?')}")

        ta     = TradingAgentsGraph(debug=False, config=config)
        layer  = ReflectionLayer(llm=ta.deep_thinking_llm)

        reports_dir  = Path("reports")
        out_dir      = reports_dir / f"reflection_{ticker}_{exit_date}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
        out_dir.mkdir(parents=True, exist_ok=True)

        log("Running reflection analysis...")
        result = layer.reflect(trade=trade, reports_dir=reports_dir, save_dir=str(out_dir))
        if result:
            log("")
            log(str(result)[:2000])

        log("")
        log(f"Reflection saved to: {out_dir.name}/")

        try:
            from cli.commands.reports import _auto_build_web
            _auto_build_web()
        except Exception:
            pass

        _finish(job_id, True)

    except Exception as exc:
        _log(job_id, f"FATAL: {exc}")
        _finish(job_id, False)


def _run_improve(job_id: str, params: dict) -> None:
    log = lambda msg: _log(job_id, msg)
    try:
        import json as _json
        from tradingagents.reflection.layer import parse_reflection_score
        from cli.commands.improve import _build_improve_prompt
        from langchain_core.messages import HumanMessage, SystemMessage

        config, _ = _build_config(params)

        from cli.commands.common import _trades_path
        trade_log_path = _trades_path()
        all_trades: list = []
        if trade_log_path.exists():
            try:
                all_trades = _json.loads(trade_log_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        reflections_dir = Path("reports") / "reflections"
        if not reflections_dir.exists():
            log("No reflections found in reports/reflections/")
            _finish(job_id, False)
            return

        items: list = []
        for d in sorted(reflections_dir.iterdir(), key=lambda p: p.name, reverse=True):
            if not d.is_dir():
                continue
            pm = d / "post_mortem.md"
            if not pm.exists():
                continue
            parts = d.name.split("_")
            if len(parts) < 3:
                continue
            ticker, exit_date = parts[0], parts[1]
            content = pm.read_text(encoding="utf-8")
            score = parse_reflection_score(content)
            trade = next((t for t in all_trades
                          if t.get("ticker", "").upper() == ticker
                          and t.get("exit_date", "") == exit_date), {})
            items.append({
                "ticker": ticker, "exit_date": exit_date, "content": content,
                "outcome":       score.get("outcome") or trade.get("outcome", "?"),
                "direction":     score.get("direction") or trade.get("direction", "?"),
                "pnl":           trade.get("pnl"),
                "pnl_pct":       score.get("pnl_pct") if score.get("pnl_pct") is not None else trade.get("pnl_pct"),
                "beat_correct":  score.get("beat_prediction_correct"),
                "guide_correct": score.get("guidance_prediction_correct"),
                "key_lesson":    score.get("key_lesson", ""),
            })

        # Deduplicate by ticker+exit_date (keep latest)
        seen: set = set()
        unique: list = []
        for item in items:
            key = (item["ticker"], item["exit_date"])
            if key not in seen:
                seen.add(key)
                unique.append(item)

        if not unique:
            log("No reflection post-mortems found.")
            _finish(job_id, False)
            return

        log(f"Found {len(unique)} reflection(s). Building improvement prompt...")
        log(f"Provider: {config['llm_provider']}  |  Model: {config['deep_think_llm']}")
        log("")

        prompt_text = _build_improve_prompt(unique)
        ta  = TradingAgentsGraph(debug=False, config=config)
        llm = ta.deep_thinking_llm
        messages = [
            SystemMessage(content=(
                "You are a systematic trading pipeline improvement expert. "
                "Analyse the trade post-mortems and produce specific, actionable "
                "recommendations. Be concrete — cite individual trades, propose exact "
                "prompt wording where helpful, and give numeric weight recommendations "
                "with justification."
            )),
            HumanMessage(content=prompt_text),
        ]

        log("Calling LLM (this may take a minute)...")
        response = llm.invoke(messages)
        output = response.content if hasattr(response, "content") else str(response)

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        save_path = Path("reports") / f"improvement_{timestamp}.md"
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(output, encoding="utf-8")

        log("")
        log(f"Saved to: {save_path.name}")
        log("")
        log("── Report (excerpt) ─────────────────────────────────")
        log(output[:3000] + ("..." if len(output) > 3000 else ""))

        try:
            from cli.commands.reports import _auto_build_web
            _auto_build_web()
        except Exception:
            pass

        _finish(job_id, True)

    except Exception as exc:
        _log(job_id, f"FATAL: {exc}")
        _finish(job_id, False)


def _run_allocate(job_id: str, params: dict) -> None:
    log = lambda msg: _log(job_id, msg)
    try:
        import json as _json

        run_name   = params.get("run_name", "").strip()
        budget     = int(params.get("budget", 100_000))
        trade_date = params.get("date", datetime.date.today().isoformat())
        analysts   = params.get("analysts", ["market", "social", "news", "fundamentals"])
        config, _  = _build_config(params)

        earnings_base = Path("reports") / "earnings"
        if run_name:
            screening_dir = earnings_base / run_name
        else:
            from tradingagents.reports_layout import iter_run_dirs
            candidates = iter_run_dirs(Path("reports"))
            if not candidates:
                log("No screening runs found.")
                _finish(job_id, False)
                return
            screening_dir = candidates[0]

        if not screening_dir.exists():
            log(f"ERROR: directory not found: {screening_dir}")
            _finish(job_id, False)
            return

        log(f"Allocating: {screening_dir.name}")
        log(f"Budget: ${budget:,}  |  Provider: {config['llm_provider']}  |  Model: {config['deep_think_llm']}")
        log("")

        # Rebuild results from saved earnings briefs
        results: list = []
        for ticker_dir in sorted(screening_dir.iterdir()):
            if not ticker_dir.is_dir():
                continue
            brief_path = ticker_dir / "earnings_brief.md"
            if not brief_path.exists():
                continue
            brief  = brief_path.read_text(encoding="utf-8")
            score  = parse_score(brief)
            fund   = {}
            fund_path = ticker_dir / "fundamentals_score.json"
            if fund_path.exists():
                try:
                    fund = _json.loads(fund_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            results.append({
                "ticker":              ticker_dir.name,
                "sector":              "Unknown",
                "ta_decision":         score.get("signal", "?"),
                "brief":               brief,
                "earnings_date":       score.get("earnings_date", "?"),
                "beat_score":          score.get("beat_score", 0),
                "guidance_score":      score.get("guidance_score", 0),
                "setup_score":         score.get("setup_score", 0),
                "total_score":         score.get("total_score", 0),
                "signal":              score.get("signal", "?"),
                "confidence":          score.get("confidence", "?"),
                "one_liner":           score.get("one_liner", ""),
                "fundamentals_score":  fund.get("fundamentals_score", 0),
                "bs_quality":          fund.get("balance_sheet", "Adequate"),
                "margin_trend":        fund.get("profitability", "Stable"),
                "growth_quality":      fund.get("growth_quality", "Medium"),
                "fundamentals_summary": fund.get("summary", ""),
            })

        if not results:
            log("No ticker results found.")
            _finish(job_id, False)
            return

        sorted_results = sorted(results, key=lambda r: r.get("total_score", 0), reverse=True)
        log(f"Found {len(sorted_results)} tickers.")

        alloc_report = run_allocation(
            sorted_results, trade_date, screening_dir, budget, config,
            analysts=analysts, progress=lambda msg: log(f"  {msg}"),
        )
        if alloc_report:
            alloc_data = parse_allocation(alloc_report)
            deployed   = alloc_data.get("total_deployed", 0)
            log(f"")
            log(f"Allocation done — deployed: ${deployed:,}")

        try:
            from cli.commands.reports import _auto_build_web
            _auto_build_web()
        except Exception:
            pass

        _finish(job_id, True)

    except Exception as exc:
        _log(job_id, f"FATAL: {exc}")
        _finish(job_id, False)


# --------------------------------------------------------------------------- #
# HTTP endpoints
# --------------------------------------------------------------------------- #

class JobRequest(BaseModel):
    type: str
    params: dict = {}


class CalendarFetchRequest(BaseModel):
    date: str  # YYYY-MM-DD trading day


class CalendarAddRequest(BaseModel):
    date: str   # YYYY-MM-DD trading day
    tickers: str  # comma-separated


class CalendarFetchRangeRequest(BaseModel):
    start: str  # YYYY-MM-DD
    end: str    # YYYY-MM-DD


@app.get("/", response_class=HTMLResponse)
async def root():
    html = (Path(__file__).parent / "static" / "reports_site.html").read_text()
    # Serve the HTML with live data embedded
    try:
        from cli.commands.reports import _build_reports_data
        import json
        from cli.commands.common import _trades_path
        data = _build_reports_data(Path("reports"), _trades_path())
        html = html.replace("__TRADINGAGENTS_DATA__", json.dumps(data))
    except Exception:
        html = html.replace("__TRADINGAGENTS_DATA__", "{}")
    return HTMLResponse(html)


# Client-side-routed views: serve the same single-page app so /screenings,
# /trades, … load directly (reload / bookmark / deep link) rather than only via
# the in-app nav. The frontend reads location.pathname to render the right view.
for _view in ("overview", "screenings", "trades", "reflections", "performance", "run"):
    app.add_api_route(f"/{_view}", root, response_class=HTMLResponse)


@app.get("/api/calendar")
async def get_calendar():
    return JSONResponse(_load_calendar())


@app.post("/api/calendar/fetch")
async def fetch_calendar(req: CalendarFetchRequest):
    try:
        loop = asyncio.get_event_loop()
        entries = await loop.run_in_executor(None, _fetch_day_calendar_entries, req.date)
        cal = _load_calendar()
        pm_date = _next_market_day(req.date)
        cal[req.date] = {
            "trading_day": req.date,
            "fetched_at": datetime.datetime.now().isoformat(),
            "ah_date": req.date,
            "pm_date": pm_date,
            "entries": entries,
        }
        _save_calendar(cal)
        return JSONResponse(cal[req.date])
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/calendar/fetch-range")
async def fetch_calendar_range(req: CalendarFetchRangeRequest):
    try:
        start = datetime.date.fromisoformat(req.start)
        end   = datetime.date.fromisoformat(req.end)
        if end < start:
            raise HTTPException(400, "end must be >= start")
        days = []
        d = start
        while d <= end:
            if d.weekday() < 5:
                days.append(d.isoformat())
            d += datetime.timedelta(days=1)
        if not days:
            raise HTTPException(400, "No trading days in range")
        loop = asyncio.get_event_loop()
        tasks = [loop.run_in_executor(None, _fetch_day_calendar_entries, day) for day in days]
        fetched = await asyncio.gather(*tasks, return_exceptions=True)
        cal = _load_calendar()
        for day, result in zip(days, fetched):
            if isinstance(result, Exception):
                continue
            pm_date = _next_market_day(day)
            if day in cal:
                # Preserve manual entries; replace auto-fetched ones
                manual = [e for e in cal[day].get("entries", []) if e.get("manual")]
                seen = {e["ticker"] for e in result}
                for m in manual:
                    if m["ticker"] not in seen:
                        result.append(m)
                        seen.add(m["ticker"])
                cal[day]["entries"] = result
                cal[day]["fetched_at"] = datetime.datetime.now().isoformat()
            else:
                cal[day] = {
                    "trading_day": day,
                    "fetched_at": datetime.datetime.now().isoformat(),
                    "ah_date": day,
                    "pm_date": pm_date,
                    "entries": result,
                }
        _save_calendar(cal)
        return JSONResponse(cal)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/calendar/add")
async def add_calendar_tickers(req: CalendarAddRequest):
    tickers = [t.strip().upper() for t in req.tickers.split(",") if t.strip()]
    if not tickers:
        raise HTTPException(400, "No tickers provided")
    cal = _load_calendar()
    pm_date = _next_market_day(req.date)
    if req.date not in cal:
        cal[req.date] = {
            "trading_day": req.date,
            "fetched_at": datetime.datetime.now().isoformat(),
            "ah_date": req.date,
            "pm_date": pm_date,
            "entries": [],
        }
    existing = {e["ticker"] for e in cal[req.date]["entries"]}
    for ticker in tickers:
        if ticker not in existing:
            cal[req.date]["entries"].append({
                "ticker": ticker, "company": "",
                "time": "?", "session": "manual", "report_date": req.date,
                "eps_estimate": None, "market_cap": None, "manual": True,
            })
            existing.add(ticker)
    _save_calendar(cal)
    return JSONResponse(cal[req.date])


@app.delete("/api/calendar/{date}/{ticker}")
async def delete_calendar_entry(date: str, ticker: str):
    cal = _load_calendar()
    if date not in cal:
        raise HTTPException(404, "Date not found")
    cal[date]["entries"] = [e for e in cal[date]["entries"] if e["ticker"] != ticker.upper()]
    _save_calendar(cal)
    return JSONResponse({"ok": True})


@app.get("/api/trades")
async def get_trades():
    from cli.commands.common import _trades_path
    trades_path = _trades_path()
    try:
        data = []
        if trades_path.exists():
            import json as _json
            data = _json.loads(trades_path.read_text(encoding="utf-8"))
        return JSONResponse(data)
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/data")
async def get_data():
    try:
        from cli.commands.reports import _build_reports_data
        import json
        from cli.commands.common import _trades_path
        data = _build_reports_data(Path("reports"), _trades_path())
        return JSONResponse(data)
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/stats")
async def get_stats():
    import json as _json
    from cli.commands.common import _trades_path
    trade_log_path = _trades_path()
    all_trades: list = []
    if trade_log_path.exists():
        try:
            all_trades = _json.loads(trade_log_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    trades_stats: dict = {}
    if all_trades:
        n = len(all_trades)
        wins   = sum(1 for t in all_trades if t.get("pnl", 0) > 0)
        losses = sum(1 for t in all_trades if t.get("pnl", 0) < 0)
        total_pnl = sum(t.get("pnl", 0) for t in all_trades)
        beat_data = [t for t in all_trades if t.get("beat_prediction_correct") is not None]
        guid_data = [t for t in all_trades if t.get("guidance_prediction_correct") is not None]
        trades_stats = {
            "count": n, "wins": wins, "losses": losses,
            "win_rate": wins / n * 100 if n else 0,
            "total_pnl": total_pnl,
            "avg_pnl": total_pnl / n if n else 0,
            "beat_accuracy": sum(1 for t in beat_data if t.get("beat_prediction_correct")) / len(beat_data) * 100 if beat_data else None,
            "guidance_accuracy": sum(1 for t in guid_data if t.get("guidance_prediction_correct")) / len(guid_data) * 100 if guid_data else None,
            "by_direction": {
                d: {
                    "count": len([t for t in all_trades if t.get("direction") == d]),
                    "wins":  sum(1 for t in all_trades if t.get("direction") == d and t.get("pnl", 0) > 0),
                }
                for d in ("BUY", "SHORT")
                if any(t.get("direction") == d for t in all_trades)
            },
        }

    cal_stats: dict = {}
    try:
        from tradingagents.calibration import load_all_calibrations
        calibrations = load_all_calibrations(Path("reports")) if Path("reports").exists() else []
        if calibrations:
            all_rows = [row for cal in calibrations for row in cal.get("rows", [])]
            with_signal = [r for r in all_rows if r.get("signal_correct") is not None]
            conf_acc: dict = {}
            for r in with_signal:
                c = r.get("confidence", "?")
                conf_acc.setdefault(c, {"correct": 0, "total": 0})
                conf_acc[c]["total"] += 1
                if r["signal_correct"]:
                    conf_acc[c]["correct"] += 1
            cal_stats = {
                "screening_runs": len(calibrations),
                "tickers_screened": len(all_rows),
                "signal_accuracy": sum(1 for r in with_signal if r["signal_correct"]) / len(with_signal) * 100 if with_signal else None,
                "with_signal": len(with_signal),
                "by_confidence": {
                    c: {"accuracy": v["correct"] / v["total"] * 100, "n": v["total"]}
                    for c, v in conf_acc.items() if v["total"] > 0
                },
            }
    except Exception:
        pass

    return JSONResponse({"trades": trades_stats, "calibration": cal_stats})


@app.get("/api/weights")
async def get_weights():
    from tradingagents.allocation.weights import load_weights
    return JSONResponse(load_weights())


class WeightsRequest(BaseModel):
    beat: float
    guidance: float
    setup: float
    fundamentals: float


@app.post("/api/weights")
async def update_weights(req: WeightsRequest):
    from tradingagents.allocation.weights import save_weights, load_weights
    save_weights(req.beat, req.guidance, req.setup, req.fundamentals)
    return JSONResponse(load_weights())


@app.get("/api/screening-runs")
async def list_screening_runs():
    earnings_base = Path("reports") / "earnings"
    if not earnings_base.exists():
        return JSONResponse([])
    runs = []
    for d in sorted(earnings_base.iterdir(), key=lambda p: p.name, reverse=True):
        if not d.is_dir():
            continue
        tickers = [t for t in d.iterdir() if t.is_dir() and (t / "earnings_brief.md").exists()]
        runs.append({
            "name":           d.name,
            "tickers":        len(tickers),
            "has_allocation": (d / "allocation.md").exists(),
        })
    return JSONResponse(runs)


@app.post("/api/jobs")
async def create_job(req: JobRequest):
    runners = {
        "screen":    _run_screen,
        "analyze":   _run_analyze,
        "calibrate": _run_calibrate,
        "reflect":   _run_reflect,
        "improve":   _run_improve,
        "allocate":  _run_allocate,
    }
    runner = runners.get(req.type)
    if not runner:
        raise HTTPException(400, f"Unknown job type: {req.type}")

    job_id = _new_job(req.type, req.params)
    with _JOBS_LOCK:
        _jobs[job_id]["status"] = "running"

    t = threading.Thread(target=runner, args=(job_id, req.params), daemon=True)
    t.start()

    return {"job_id": job_id}


@app.get("/api/jobs")
async def list_jobs():
    with _JOBS_LOCK:
        summary = [
            {"id": j["id"], "type": j["type"], "status": j["status"]}
            for j in _jobs.values()
        ]
    return JSONResponse(summary)


@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str):
    with _JOBS_LOCK:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {"id": job["id"], "type": job["type"], "status": job["status"], "log": job["log"]}


@app.websocket("/ws/{job_id}")
async def websocket_job(ws: WebSocket, job_id: str):
    await ws.accept()
    with _JOBS_LOCK:
        job = _jobs.get(job_id)
    if not job:
        await ws.send_text("ERROR: Job not found")
        await ws.close()
        return

    log_q = job["queue"]
    loop  = asyncio.get_event_loop()

    # Flush already-buffered lines first
    with _JOBS_LOCK:
        buffered = list(job["log"])
    for msg in buffered:
        await ws.send_text(msg)

    # Then stream new lines as they arrive
    try:
        while True:
            try:
                msg = await loop.run_in_executor(None, lambda: log_q.get(timeout=0.5))
                await ws.send_text(msg)
                if msg in ("__DONE__", "__ERROR__"):
                    break
            except queue.Empty:
                with _JOBS_LOCK:
                    status = _jobs.get(job_id, {}).get("status", "")
                if status in ("done", "error"):
                    break
    except WebSocketDisconnect:
        pass


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="info")
