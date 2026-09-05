"""TradingAgents web server — FastAPI backend for the reports dashboard and job runner."""

import asyncio
import datetime
import queue
import threading
import time
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

# Human labels for the activity banner; also used by the durable registry.
_JOB_LABELS = {
    "screen": "Screening", "analyze": "Analysis", "calibrate": "Calibration",
    "reflect": "Reflection", "improve": "Improvement", "allocate": "Allocation",
    "resume": "Resume",
}

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
    # Mirror into the durable registry so the job stays visible if this process
    # dies — the in-memory dict above does not survive a restart.
    try:
        from cli import jobs_registry
        jobs_registry.register(job_type, job_id=job_id, params=params,
                               label=_JOB_LABELS.get(job_type, job_type),
                               reports_root=Path("reports"))
    except Exception:
        pass
    return job_id


def _log(job_id: str, msg: str) -> None:
    with _JOBS_LOCK:
        job = _jobs.get(job_id)
    if job:
        job["log"].append(msg)
        job["queue"].put(msg)


def _job_cancelled(job_id: str) -> bool:
    """Has a stop been requested for this job? Safe to call from any worker."""
    try:
        from cli import jobs_registry
        return jobs_registry.is_cancelled(job_id, reports_root=Path("reports"))
    except Exception:
        return False


def _finish(job_id: str, success: bool = True, cancelled: bool = False) -> None:
    sentinel = "__DONE__" if success else "__ERROR__"
    cancelled = cancelled or _job_cancelled(job_id)
    with _JOBS_LOCK:
        job = _jobs.get(job_id)
    if job:
        job["status"] = "cancelled" if cancelled else ("done" if success else "error")
        job["queue"].put(sentinel)
    try:
        from cli import jobs_registry
        status = (jobs_registry.CANCELLED if cancelled
                  else jobs_registry.DONE if success else jobs_registry.ERROR)
        jobs_registry.finish(job_id, status, reports_root=Path("reports"))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Config helpers
# --------------------------------------------------------------------------- #

def _build_config(params: dict) -> tuple[dict, list[str]]:
    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = params.get("provider", "deepseek").lower()
    config["quick_think_llm"] = params.get("quick_model", "deepseek-v4-flash")
    # v4-pro is safe again now that DeepSeek calls stream by default; it drops
    # ~2/3 of long generations mid-response when not streamed (model_catalog.py).
    config["deep_think_llm"] = params.get("deep_model", "deepseek-v4-pro")
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
            # The submitted list is the only authoritative record of what this
            # run was meant to cover — a filtered subset of a calendar day is
            # indistinguishable from a run that died early once it is lost.
            "tickers": tickers,
            "earnings_date": earnings_date or None,
            "trade_date": trade_date,
            "depth": int(params.get("depth", 1)),
            "provider": config.get("llm_provider", ""),
            "quick_model": config.get("quick_think_llm", ""),
            "deep_model": config.get("deep_think_llm", ""),
            "run_at": datetime.datetime.now().isoformat(),
        }
        (screening_dir / "metadata.json").write_text(_json_meta.dumps(_meta, indent=2), encoding="utf-8")

        # Now that the folder and ticker count exist, the registry can report progress.
        try:
            from cli import jobs_registry
            jobs_registry.update(job_id, reports_root=Path("reports"),
                                 run_dir=str(screening_dir), total=len(tickers),
                                 label=f"Screening {screening_dir.name}")
        except Exception:
            pass

        log(f"Tickers ({len(tickers)}): {', '.join(tickers)}")
        log(f"Date: {trade_date}  |  Earnings date: {earnings_date or '—'}  |  Depth: {params.get('depth', 1)}  |  Workers: {workers}  |  Budget: ${budget:,}")
        log(f"Provider: {config['llm_provider']}  |  Models: {config['quick_think_llm']} / {config['deep_think_llm']}")
        log(f"Output: {screening_dir.name}/")
        log("")

        results: list[dict] = []
        results_lock = threading.Lock()

        def process(ticker: str, worker_config: dict) -> None:
            # Cooperative stop: a queued ticker is simply never started. One
            # already inside screen_ticker runs to completion — we cannot
            # interrupt a thread, and killing the PID would kill the server.
            if _job_cancelled(job_id):
                log(f"[{ticker}] skipped — stop requested")
                return
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

        # Stopped part-way: keep the table for whatever did finish (it is what
        # `resume` reads back), but do not spend an 11-call council on a
        # deliberately partial screen.
        stopped = _job_cancelled(job_id)

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
        if stopped:
            log(f"Stopped by request — {len(sorted_results)} of {len(tickers)} screened, "
                f"allocation skipped. Use Resume to finish this run.")
            _finish(job_id, True, cancelled=True)
            return
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


def _run_resume(job_id: str, params: dict) -> None:
    """Finish an interrupted run: screen what's missing, rebuild the table, allocate.

    Thin wrapper — the work lives in `cli.commands.resume` so the CLI `resume`
    command and this endpoint can never drift.
    """
    log = lambda msg: _log(job_id, msg)
    try:
        from cli.commands.resume import DEFAULT_BUDGET, DEFAULT_WORKERS, plan_resume, resume_run

        run_name = (params.get("run_name") or params.get("run_dir") or "").strip()
        if not run_name:
            log("ERROR: no run specified.")
            _finish(job_id, False)
            return

        run_dir = Path(run_name)
        if not run_dir.exists():                       # accept a bare folder name too
            run_dir = Path("reports") / "earnings" / run_name
        if not run_dir.exists():
            log(f"ERROR: run folder not found: {run_name}")
            _finish(job_id, False)
            return

        plan = plan_resume(run_dir, universe=params.get("universe", "auto"))
        if not plan["missing"] and plan["has_allocation"] and not params.get("force"):
            log(f"{plan['name']} is already complete ({len(plan['done'])}/{plan['total']} "
                f"screened, allocation present). Nothing to do.")
            _finish(job_id, True)
            return

        overrides = {}
        if params.get("provider"):
            cfg, _ = _build_config(params)             # explicit overrides win over run metadata
            overrides = {k: cfg[k] for k in ("llm_provider", "quick_think_llm", "deep_think_llm")}

        resume_run(
            run_dir,
            budget=int(params.get("budget", DEFAULT_BUDGET)),
            workers=int(params.get("workers", DEFAULT_WORKERS)),
            allocate=not params.get("no_allocate"),
            universe=params.get("universe", "auto"),
            config_overrides=overrides or None,
            log=log,
            job_id=job_id,
            reports_root=Path("reports"),
        )
        try:
            from cli.commands.reports import _auto_build_web
            _auto_build_web()
        except Exception:
            pass
        _finish(job_id, True)
    except Exception as exc:
        log(f"ERROR: {exc}")
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


# NOTE: these handlers are deliberately `def`, not `async def`. Their bodies are
# synchronous (file reads, yfinance calls, report building), and Starlette runs a
# sync handler in a threadpool while an async one runs *on the event loop* — so an
# `async def` that blocks stalls every other request. /api/watchlist takes ~8 s on
# a cold quote cache; as `async def` it made the Screenings page's 2 ms calendar
# request appear to take five seconds.
@app.get("/", response_class=HTMLResponse)
def root():
    html = (Path(__file__).parent / "static" / "reports_site.html").read_text()
    # Embed only the first page of data — the rest arrives via /api/runs,
    # /api/trades and /api/report as the user asks for it.
    try:
        from cli.commands.reports import _build_reports_data
        import json
        from cli.commands.common import _trades_path
        data = _build_reports_data(
            Path("reports"), _trades_path(),
            limit_runs=INITIAL_RUNS, include_bodies=False,
            limit_trades=INITIAL_TRADES, limit_reflections=INITIAL_REFLECTIONS,
            refresh_watchlist=False,
        )
        # …but serve live statuses from the warm snapshot, so TRIGGERED rows and
        # the overview alert are right on the first paint instead of appearing a
        # beat later. Costs nothing: it never blocks on a fetch.
        data["watchlist"] = watchlist_snapshot(block_if_cold=False) or data["watchlist"]
        html = html.replace("__TRADINGAGENTS_DATA__", json.dumps(data))
    except Exception:
        html = html.replace("__TRADINGAGENTS_DATA__", "{}")
    return HTMLResponse(html)


# Client-side-routed views: serve the same single-page app so /screenings,
# /trades, … load directly (reload / bookmark / deep link) rather than only via
# the in-app nav. The frontend reads location.pathname to render the right view.
for _view in ("overview", "screenings", "analyses", "watchlist", "trades", "reflections", "performance", "run"):
    app.add_api_route(f"/{_view}", root, response_class=HTMLResponse)


@app.get("/api/calendar")
def get_calendar():
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
def add_calendar_tickers(req: CalendarAddRequest):
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
def get_trades(limit: int | None = None, offset: int = 0):
    """Trades, newest-last as stored. `limit` returns the most recent N.

    `offset` walks further back from there (offset=300&limit=300 is "the 300
    before the 300 you already have"), which is how the Trades view pages.
    Without params the whole log comes back, as it always did.
    """
    from cli.commands.common import _trades_path
    trades_path = _trades_path()
    try:
        data = []
        if trades_path.exists():
            import json as _json
            data = _json.loads(trades_path.read_text(encoding="utf-8"))
        total = len(data)
        if limit is not None:
            end = max(0, total - max(0, offset))
            data = data[max(0, end - max(0, limit)):end]
            return JSONResponse({"trades": data, "total": total, "offset": offset})
        return JSONResponse(data)
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/reflections")
def get_reflections(limit: int = 40, offset: int = 0):
    """Reflections newest-first, paged — bodies included (they're small)."""
    try:
        from cli.commands.reports import _build_reports_data
        from cli.commands.common import _trades_path
        data = _build_reports_data(Path("reports"), _trades_path(),
                                   limit_runs=0, include_bodies=False, limit_trades=0,
                                   refresh_watchlist=False)
        refl = data["reflections"]
        return JSONResponse({
            "reflections": refl[offset:offset + max(1, limit)],
            "total": len(refl), "offset": offset,
        })
    except Exception as exc:
        raise HTTPException(500, str(exc))


# How much the dashboard loads before you ask for more. Report bodies are never
# in the first payload — they were ~20 MB of a 27 MB page (see _build_reports_data).
INITIAL_RUNS = 12
# Trades are NOT truncated. The Overview builds its stats, charts and equity
# curve client-side from DATA.trades, so a partial log silently reports partial
# lifetime P&L ($28k of $143k) rather than looking incomplete. At ~800 B a fill
# the whole log is under 1 MB — a rounding error next to the report bodies that
# actually made the page heavy. /api/trades?limit=&offset= still exists for
# callers that want a page.
INITIAL_TRADES = None
INITIAL_REFLECTIONS = 40
PAGE_RUNS = 12


@app.get("/api/data")
def get_data(full: bool = False):
    """First payload for the dashboard: recent runs only, no report bodies.

    `full=true` returns everything, which is what the static `build-web` output
    needs — there is no server behind that page to fetch the rest from.
    """
    try:
        from cli.commands.reports import _build_reports_data
        from cli.commands.common import _trades_path
        kwargs = {} if full else dict(
            limit_runs=INITIAL_RUNS, include_bodies=False,
            limit_trades=INITIAL_TRADES, limit_reflections=INITIAL_REFLECTIONS,
            refresh_watchlist=False,
        )
        data = _build_reports_data(Path("reports"), _trades_path(), **kwargs)
        if not full:  # same warm snapshot the page embed uses
            data["watchlist"] = watchlist_snapshot(block_if_cold=False) or data["watchlist"]
        return JSONResponse(data)
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/runs")
def get_runs(offset: int = 0, limit: int = PAGE_RUNS):
    """The next page of screening runs (summaries only, no report bodies)."""
    try:
        from cli.commands.reports import _build_reports_data
        from cli.commands.common import _trades_path
        data = _build_reports_data(
            Path("reports"), _trades_path(),
            limit_runs=max(1, min(limit, 100)), run_offset=max(0, offset),
            include_bodies=False, limit_trades=0, limit_reflections=0,
            refresh_watchlist=False,
        )
        return JSONResponse({
            "runs":       data["screening_runs"],
            "total_runs": data["total_runs"],
            "offset":     data["run_offset"],
        })
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/report")
def get_report(run: str, ticker: str):
    """One ticker's markdown bodies, fetched only when a report is opened."""
    try:
        run_dir = Path(run)
        if not run_dir.exists():
            run_dir = Path("reports") / "earnings" / run
        ticker_dir = run_dir / ticker
        if not ticker_dir.is_dir():
            raise HTTPException(404, f"no such ticker report: {run}/{ticker}")

        def _read(path: Path, cap: int):
            if not path.exists():
                return None
            raw = path.read_text(encoding="utf-8")
            return raw[:cap] if len(raw) > cap else raw

        return JSONResponse({
            "ticker": ticker,
            "earnings_brief_md":     _read(ticker_dir / "earnings_brief.md", 15_000),
            "portfolio_decision_md": _read(ticker_dir / "5_portfolio" / "decision.md", 12_000),
        })
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, str(exc))


# --------------------------------------------------------------------------- #
# Watchlist snapshot (stale-while-revalidate)
# --------------------------------------------------------------------------- #
# TRIGGERED is the one status that cannot be known without a live quote, so a
# page served from unrefreshed data paints ARMED rows that flip a second later —
# a visible stutter, and the overview's alert strip arriving late is exactly the
# thing a user needs promptly. Keeping one warm snapshot in the server means the
# embedded payload is already live-accurate and /api/watchlist answers instantly.
# Requests never block on the refresh: they take what is cached and trigger a
# background one, so a slow yfinance call can never sit on the critical path.

_WL_TTL_SECONDS = 45.0
_WL_LOCK = threading.Lock()
_WL_SNAP: dict = {"at": 0.0, "data": None}
_WL_REFRESHING = False


def _wl_compute() -> list[dict]:
    from tradingagents.allocation.watchlist import collect_watchlists
    return collect_watchlists(Path("reports"))


def _wl_refresh_bg() -> None:
    """Recompute the snapshot off the request path. At most one at a time."""
    global _WL_REFRESHING
    with _WL_LOCK:
        if _WL_REFRESHING:
            return
        _WL_REFRESHING = True

    def work():
        global _WL_REFRESHING
        try:
            data = _wl_compute()
            with _WL_LOCK:
                _WL_SNAP["data"], _WL_SNAP["at"] = data, time.time()
        except Exception:
            pass
        finally:
            with _WL_LOCK:
                _WL_REFRESHING = False

    threading.Thread(target=work, daemon=True).start()


def watchlist_snapshot(block_if_cold: bool = True) -> list[dict]:
    """Cached watchlist, refreshed in the background when stale.

    Only the very first caller after startup pays for the fetch (and only if
    ``block_if_cold``); everyone after that gets the snapshot immediately.
    """
    with _WL_LOCK:
        data, at = _WL_SNAP["data"], _WL_SNAP["at"]
    if data is not None:
        if time.time() - at > _WL_TTL_SECONDS:
            _wl_refresh_bg()
        return data
    if not block_if_cold:
        _wl_refresh_bg()
        return []
    try:
        data = _wl_compute()
    except Exception:
        return []
    with _WL_LOCK:
        _WL_SNAP["data"], _WL_SNAP["at"] = data, time.time()
    return data


@app.on_event("startup")
def _warm_watchlist():
    """Fill the snapshot before the first page asks for it."""
    _wl_refresh_bg()


@app.get("/api/watchlist")
def get_watchlist():
    """Watchlist entries (#19) with freshly computed trigger status."""
    try:
        return JSONResponse(watchlist_snapshot())
    except Exception as exc:
        raise HTTPException(500, str(exc))


class WatchlistActionRequest(BaseModel):
    run_id: str
    ticker: str
    action: str            # purchased | dismissed | reset
    note: str | None = None


@app.post("/api/watchlist/action")
def watchlist_action(req: WatchlistActionRequest):
    """Flag a watch entry as bought, drop it, or undo either (#19).

    Orders are still placed manually — this only records what the user did, so
    the row stops nagging. Returns the refreshed list for a straight re-render.
    """
    from tradingagents.allocation.watchlist import (
        USER_STATES,
        collect_watchlists,
        fetch_live_price,
        set_entry_state,
    )

    action = (req.action or "").strip().lower()
    if action not in USER_STATES + ("reset",):
        raise HTTPException(400, f"unknown action {req.action!r}")
    try:
        price = fetch_live_price(req.ticker) if action == "purchased" else None
        set_entry_state(
            req.run_id,
            req.ticker,
            None if action == "reset" else action,
            reports_root=Path("reports"),
            note=req.note,
            price=price,
        )
        # The overlay just changed, so the snapshot is wrong by definition —
        # recompute inline (caches are warm) and publish it for everyone else.
        data = collect_watchlists(Path("reports"))
        with _WL_LOCK:
            _WL_SNAP["data"], _WL_SNAP["at"] = data, time.time()
        return JSONResponse(data)
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/stats")
def get_stats():
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
def get_weights():
    from tradingagents.allocation.weights import load_weights
    return JSONResponse(load_weights())


class WeightsRequest(BaseModel):
    beat: float
    guidance: float
    setup: float
    fundamentals: float


@app.post("/api/weights")
def update_weights(req: WeightsRequest):
    from tradingagents.allocation.weights import save_weights, load_weights
    save_weights(req.beat, req.guidance, req.setup, req.fundamentals)
    return JSONResponse(load_weights())


@app.get("/api/screening-runs")
def list_screening_runs():
    # iter_run_dirs, not a raw name sort: sorting on the name puts every
    # screening_ run above every earnings_ one ('s' > 'e') regardless of date.
    from tradingagents.reports_layout import iter_run_dirs

    runs = []
    for d in iter_run_dirs(Path("reports")):
        tickers = [t for t in d.iterdir() if t.is_dir() and (t / "earnings_brief.md").exists()]
        runs.append({
            "name":           d.name,
            "tickers":        len(tickers),
            "has_allocation": (d / "allocation.md").exists(),
        })
    return JSONResponse(runs)


@app.post("/api/jobs")
def create_job(req: JobRequest):
    runners = {
        "screen":    _run_screen,
        "analyze":   _run_analyze,
        "calibrate": _run_calibrate,
        "reflect":   _run_reflect,
        "improve":   _run_improve,
        "allocate":  _run_allocate,
        "resume":    _run_resume,
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


@app.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str, force: bool = False):
    """Ask a job to stop, by whatever means is safe for where it is running.

    Three cases, and the distinction matters:

    * **This process** (the usual one — dashboard jobs are threads here). Their
      recorded PID *is* the server's, so signalling it would kill the dashboard.
      Cancellation is cooperative: queued tickers are never started, in-flight
      ones finish. Reported as `cancelling` until the work unwinds.
    * **Another live process** — a detached CLI run. SIGTERM (SIGKILL on
      ``force``), which the registry then observes via PID liveness.
    * **A process that is already gone.** Nothing to stop; the record is just
      marked so it stops claiming to be running.
    """
    import os
    import signal

    from cli import jobs_registry

    rec = next((r for r in jobs_registry.load(Path("reports")) if r.get("id") == job_id), None)
    with _JOBS_LOCK:
        local = _jobs.get(job_id)
    if rec is None and local is None:
        raise HTTPException(404, f"unknown job {job_id}")

    if rec and rec.get("status") in jobs_registry.TERMINAL:
        # `interrupted` is *derived* from a dead PID and not yet on disk. Persist
        # whatever it settled on, so a stopped-then-clicked job stops coming back
        # as "running" the next time the file is read.
        jobs_registry.finish(job_id, rec["status"], reports_root=Path("reports"))
        return {"status": rec["status"], "message": f"Job already {rec['status']} — cleared."}

    jobs_registry.request_cancel(job_id, reports_root=Path("reports"))

    pid = (rec or {}).get("pid")
    if pid and pid != os.getpid():
        alive = jobs_registry._alive(pid)
        if alive is False:
            jobs_registry.finish(job_id, jobs_registry.CANCELLED, reports_root=Path("reports"))
            return {"status": jobs_registry.CANCELLED,
                    "message": "That job's process was already gone — cleared."}
        try:
            os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
        except Exception as exc:
            raise HTTPException(500, f"could not signal pid {pid}: {exc}")
        return {"status": jobs_registry.CANCELLING,
                "message": f"Sent {'SIGKILL' if force else 'SIGTERM'} to pid {pid}."}

    if local is None:
        # Ours by PID, but this process has no thread for it — it belongs to a
        # previous server lifetime that happened to be assigned the same PID.
        jobs_registry.finish(job_id, jobs_registry.CANCELLED, reports_root=Path("reports"))
        return {"status": jobs_registry.CANCELLED, "message": "Cleared — no live work found."}

    _log(job_id, "── Stop requested — no further tickers will be started. ──")
    return {"status": jobs_registry.CANCELLING,
            "message": "Stopping. Tickers already in progress finish first "
                       "(up to a few minutes); nothing new starts."}


@app.get("/api/jobs")
def list_jobs():
    """Jobs this process knows about, merged with the durable registry.

    The registry contributes work this process did not start — a detached CLI
    run, or a job from a previous server lifetime — so the activity banner
    reflects everything actually running on the machine, not just what this
    process happens to remember. Registry records also carry live progress and
    an `interrupted` status for jobs whose process is gone.
    """
    with _JOBS_LOCK:
        summary = [
            {"id": j["id"], "type": j["type"], "status": j["status"], "live": True}
            for j in _jobs.values()
        ]
    known = {j["id"] for j in summary}

    try:
        from cli import jobs_registry
        by_id = {r["id"]: r for r in jobs_registry.load(Path("reports"))}
        for j in summary:                       # enrich in-process jobs with progress/label
            rec = by_id.get(j["id"])
            if rec:
                j["progress"] = rec.get("progress")
                j["label"] = rec.get("label") or _JOB_LABELS.get(j["type"], j["type"])
                j["run_dir"] = rec.get("run_dir")
                j["started_at"] = rec.get("started_at")
                # The in-memory thread still calls itself "running" while it
                # unwinds; the registry knows a stop was asked for.
                if rec.get("status") == jobs_registry.CANCELLING and j["status"] == "running":
                    j["status"] = jobs_registry.CANCELLING
        for rec in by_id.values():
            if rec["id"] in known:
                continue
            summary.append({
                "id": rec["id"], "type": rec.get("type", "job"),
                "status": rec.get("status", "running"), "live": False,
                "progress": rec.get("progress"),
                "label": rec.get("label") or _JOB_LABELS.get(rec.get("type", ""), rec.get("type", "job")),
                "run_dir": rec.get("run_dir"), "started_at": rec.get("started_at"),
                "log_path": rec.get("log_path"), "external": True,
            })
    except Exception:
        pass
    return JSONResponse(summary)


# Screening index for the calendar, cached against the run set so a finished
# screen shows up immediately but repeat page renders cost nothing.
_SCR_INDEX_CACHE: dict = {}


def _runs_signature() -> tuple:
    from tradingagents.reports_layout import iter_run_dirs
    dirs = iter_run_dirs(Path("reports"))
    newest = max((d.stat().st_mtime for d in dirs), default=0)
    return (len(dirs), round(newest))


@app.get("/api/screening-index")
def screening_index(since: str = ""):
    """ticker -> past screenings, for the earnings calendar.

    Scoped to companies on the stored calendar and independent of the paged run
    list, so the calendar shows a ticker's screening history whether or not the
    run that produced it has been loaded into the page.
    """
    try:
        from cli.commands.reports import build_screening_index

        cal_tickers = {
            str(e.get("ticker", "")).strip().upper()
            for day in (_load_calendar() or {}).values()
            for e in (day.get("entries") or [])
        } - {""}

        key = (since, _runs_signature(), len(cal_tickers))
        hit = _SCR_INDEX_CACHE.get("k")
        if hit == key and "v" in _SCR_INDEX_CACHE:
            return JSONResponse(_SCR_INDEX_CACHE["v"])

        idx = build_screening_index(Path("reports"), since=since,
                                    tickers=cal_tickers or None)
        _SCR_INDEX_CACHE.clear()
        _SCR_INDEX_CACHE["k"] = key
        _SCR_INDEX_CACHE["v"] = idx
        return JSONResponse(idx)
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/resume/candidates")
def resume_candidates():
    """Runs that are missing tickers or an allocation, newest first."""
    try:
        from cli.commands.resume import resumable_runs
        return JSONResponse(resumable_runs(Path("reports")))
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/resume/plan")
def resume_plan(run: str, universe: str = "auto"):
    """Exactly what resuming one run would do — no side effects."""
    try:
        from cli.commands.resume import plan_resume
        run_dir = Path(run)
        if not run_dir.exists():
            run_dir = Path("reports") / "earnings" / run
        if not run_dir.exists():
            raise HTTPException(404, f"run not found: {run}")
        plan = plan_resume(run_dir, universe=universe)
        plan["done"] = len(plan["done"])          # counts are all the UI needs
        plan.pop("metadata", None)
        return JSONResponse(plan)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/jobs/{job_id}")
def get_job_status(job_id: str):
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
