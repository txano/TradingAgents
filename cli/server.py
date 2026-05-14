"""TradingAgents web server — FastAPI backend for the reports dashboard and job runner."""

import asyncio
import datetime
import os
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
from tradingagents.allocation import AllocationLayer
from tradingagents.allocation.layer import parse_allocation

app = FastAPI(title="TradingAgents", docs_url=None, redoc_url=None)

# --------------------------------------------------------------------------- #
# Job registry
# --------------------------------------------------------------------------- #

_jobs: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


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

_PROVIDER_KEY_ENV = {
    "deepseek":   "DEEPSEEK_API_KEY",
    "xai":        "XAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "qwen":       "DASHSCOPE_API_KEY",
    "glm":        "ZHIPU_API_KEY",
    "openai":     "OPENAI_API_KEY",
    "anthropic":  "ANTHROPIC_API_KEY",
}


def _build_config(params: dict) -> tuple[dict, list[str]]:
    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = params.get("provider", "deepseek").lower()
    config["quick_think_llm"] = params.get("quick_model", "deepseek-chat")
    config["deep_think_llm"] = params.get("deep_model", "deepseek-chat")
    config["max_debate_rounds"] = int(params.get("depth", 1))
    config["max_risk_discuss_rounds"] = int(params.get("depth", 1))
    config["backend_url"] = params.get("backend_url") or None

    base_env = _PROVIDER_KEY_ENV.get(config["llm_provider"], "")
    api_keys: list[str] = []
    if base_env:
        for suffix in ["", "_2", "_3", "_4", "_5", "_6", "_7", "_8"]:
            k = os.environ.get(base_env + suffix, "").strip()
            if k and k not in api_keys:
                api_keys.append(k)

    return config, api_keys


def _fetch_sector(ticker: str) -> str:
    try:
        import yfinance as yf
        return yf.Ticker(ticker).info.get("sector") or "Unknown"
    except Exception:
        return "Unknown"


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

        trade_date = params.get("date", datetime.date.today().isoformat())
        analysts   = params.get("analysts", ["market", "social", "news", "fundamentals"])
        budget     = int(params.get("budget", 100_000))
        workers    = int(params.get("workers", 1))

        config, api_keys = _build_config(params)

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        screening_dir = Path("reports") / f"screening_{trade_date}_{timestamp}"
        screening_dir.mkdir(parents=True, exist_ok=True)

        log(f"Tickers ({len(tickers)}): {', '.join(tickers)}")
        log(f"Date: {trade_date}  |  Depth: {params.get('depth', 1)}  |  Workers: {workers}  |  Budget: ${budget:,}")
        log(f"Provider: {config['llm_provider']}  |  Models: {config['quick_think_llm']} / {config['deep_think_llm']}")
        log(f"Output: {screening_dir.name}/")
        log("")

        results: list[dict] = []
        results_lock = threading.Lock()

        def process(ticker: str, worker_config: dict) -> None:
            ticker_dir = screening_dir / ticker
            ticker_dir.mkdir(exist_ok=True)
            sector = _fetch_sector(ticker)
            log(f"[{ticker}] Starting...")
            try:
                ta = TradingAgentsGraph(analysts, debug=False, config=worker_config)
                final_state, decision = ta.propagate(ticker, trade_date)

                from cli.main import save_report_to_disk
                save_report_to_disk(final_state, ticker, ticker_dir)

                layer = EarningsLayer(llm=ta.deep_thinking_llm, news_lookback_days=90)
                brief, score = layer.analyze_and_score(
                    ticker, trade_date, final_state, save_dir=str(ticker_dir)
                )
                result = {
                    "ticker": ticker, "sector": sector, "ta_decision": decision,
                    "brief": brief, "earnings_date": score.get("earnings_date", "unknown"),
                    "beat_score": score.get("beat_score", 0),
                    "guidance_score": score.get("guidance_score", 0),
                    "setup_score": score.get("setup_score", 0),
                    "total_score": score.get("total_score", 0),
                    "signal": score.get("signal", "?"),
                    "confidence": score.get("confidence", "?"),
                    "one_liner": score.get("one_liner", ""),
                }
                tot = result["total_score"]
                log(f"[{ticker}] Done → {result['signal']}  score: {tot:+d}")
            except Exception as exc:
                result = {
                    "ticker": ticker, "sector": sector, "ta_decision": "ERROR",
                    "brief": "", "earnings_date": "unknown", "beat_score": 0,
                    "guidance_score": 0, "setup_score": 0, "total_score": -99,
                    "signal": "ERROR", "confidence": "—", "one_liner": str(exc),
                }
                log(f"[{ticker}] ERROR: {exc}")
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
        table_lines = [
            f"# Earnings Screener — {trade_date}\n\n",
            "| # | Ticker | Sector | Earnings | Beat | Guidance | Setup | Total | Signal | Confidence | One-liner |\n",
            "|---|--------|--------|----------|------|----------|-------|-------|--------|------------|-----------|\n",
        ]
        for i, r in enumerate(sorted_results, 1):
            table_lines.append(
                f"| {i} | {r['ticker']} | {r.get('sector','Unknown')} | {r.get('earnings_date','?')} "
                f"| {r.get('beat_score',0):+d} | {r.get('guidance_score',0):+d} "
                f"| {r.get('setup_score',0):+d} | {r.get('total_score',0):+d} "
                f"| {r.get('signal','?')} | {r.get('confidence','?')} "
                f"| {r.get('one_liner','')} |\n"
            )
        (screening_dir / "screening_table.md").write_text("".join(table_lines), encoding="utf-8")

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
            ta_alloc = TradingAgentsGraph(analysts, debug=False, config=config)
            alloc_layer = AllocationLayer(llm=ta_alloc.deep_thinking_llm, budget=budget)
            allocation_report = alloc_layer.allocate(
                results=sorted_results,
                trade_date=trade_date,
                screening_dir=screening_dir,
                save=True,
                progress_cb=lambda msg: log(f"  {msg}"),
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
            from cli.main import _auto_build_web
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
        out_dir = Path("reports") / f"{ticker}_{timestamp}"
        out_dir.mkdir(parents=True, exist_ok=True)

        from cli.main import save_report_to_disk
        save_report_to_disk(final_state, ticker, out_dir)

        log(f"[{ticker}] Decision: {decision}")
        log(f"Report saved to: {out_dir.name}/")

        try:
            from cli.main import _auto_build_web
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

        if run_name:
            target = reports_dir / run_name
        else:
            # Pick the most recent uncalibrated run
            all_runs = sorted(reports_dir.glob("screening_*/"), key=lambda p: p.name, reverse=True)
            candidates = [r for r in all_runs
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
            from cli.main import _auto_build_web
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

        trade_log_path = Path.home() / ".tradingagents" / "trades.json"
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
            from cli.main import _auto_build_web
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


@app.get("/", response_class=HTMLResponse)
async def root():
    html = (Path(__file__).parent / "static" / "reports_site.html").read_text()
    # Serve the HTML with live data embedded
    try:
        from cli.main import _build_reports_data
        import json
        data = _build_reports_data(Path("reports"), Path.home() / ".tradingagents" / "trades.json")
        html = html.replace("__TRADINGAGENTS_DATA__", json.dumps(data))
    except Exception:
        html = html.replace("__TRADINGAGENTS_DATA__", "{}")
    return HTMLResponse(html)


@app.get("/api/trades")
async def get_trades():
    trades_path = Path.home() / ".tradingagents" / "trades.json"
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
        from cli.main import _build_reports_data
        import json
        data = _build_reports_data(Path("reports"), Path.home() / ".tradingagents" / "trades.json")
        return JSONResponse(data)
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/jobs")
async def create_job(req: JobRequest):
    runners = {
        "screen":    _run_screen,
        "analyze":   _run_analyze,
        "calibrate": _run_calibrate,
        "reflect":   _run_reflect,
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
