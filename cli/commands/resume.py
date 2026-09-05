"""Resume an interrupted screening run (`tradingagents resume` + `/api/jobs` type `resume`).

A batch screen is a long, expensive, *resumable* piece of work: every ticker
writes its own artifacts, so a run killed at ticker 54 of 149 has 53 tickers of
perfectly good output on disk. Before this module the only way to recover was to
re-run everything from scratch into a new folder.

Resuming means three things, in order:

1. work out what is still missing (:func:`plan_resume`),
2. screen exactly those tickers *into the same folder*, via the same
   ``screen_ticker`` the CLI and server use, so resumed artifacts are
   indistinguishable from original ones,
3. rebuild ``screening_table.md`` from every brief and run the allocation
   council over the complete set.

What the run was *meant* to cover comes from `metadata.json["tickers"]` when the
run recorded it, else the folders it created; the earnings calendar is a
superset guess available on request but never by default — see `plan_resume`.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Optional

import typer

CALENDAR_PATH = Path.home() / ".tradingagents" / "calendar.json"

DEFAULT_WORKERS = 16
DEFAULT_BUDGET = 100_000


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #

def _calendar_universe(earnings_date: str | None) -> list[str]:
    """Tickers the run was launched over, in the order they were submitted."""
    if not earnings_date:
        return []
    try:
        day = json.loads(CALENDAR_PATH.read_text(encoding="utf-8")).get(earnings_date) or {}
    except Exception:
        return []
    out: list[str] = []
    for e in day.get("entries", []):
        t = str(e.get("ticker", "")).strip().upper()
        if t and t not in out:
            out.append(t)
    return out


def _run_metadata(run_dir: Path) -> dict:
    try:
        return json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def plan_resume(run_dir: str | Path, universe: str = "auto") -> dict:
    """Describe what finishing this run would take. Pure: reads disk, no network.

    ``missing`` keeps the original submission order, so a resumed run continues
    where it left off rather than in an arbitrary one.

    Which tickers *should* exist is the delicate part, because guessing high
    spends the user's money on names they never asked for. Precedence:

    1. ``metadata.json["tickers"]`` — what was actually submitted. Authoritative.
    2. The run's own subfolders — every attempted ticker gets one, so this
       recovers a run that failed mid-flight without inventing new work.
    3. The earnings calendar — a *superset* guess, right only when the run took
       a whole day unfiltered. Never the default: a 2026-08-12 run of 84
       hand-filtered tickers planned 128 under this rule, 84 of them unwanted.

    ``universe="calendar"`` opts into (3) explicitly. The returned
    ``calendar_extra`` always reports what (3) would add, so a caller can offer
    the choice instead of silently making it.
    """
    run_dir = Path(run_dir)
    meta = _run_metadata(run_dir)

    earnings_date = meta.get("earnings_date")
    if not earnings_date and run_dir.name.startswith("earnings_"):
        parts = run_dir.name.split("_")
        earnings_date = parts[1] if len(parts) > 1 else None

    trade_date = meta.get("trade_date")
    if not trade_date:
        parts = run_dir.name.split("_")
        trade_date = parts[1] if len(parts) > 1 else str(date.today())

    subdirs = sorted(d.name for d in run_dir.iterdir() if d.is_dir()) if run_dir.exists() else []
    done = sorted(d for d in subdirs if (run_dir / d / "earnings_brief.md").exists())

    submitted = [str(t).strip().upper() for t in (meta.get("tickers") or []) if str(t).strip()]
    calendar = _calendar_universe(earnings_date)

    if universe == "calendar" and calendar:
        pool, source = calendar, "calendar"
    elif submitted:
        pool, source = submitted, "metadata"
    elif subdirs:
        pool, source = subdirs, "folder"
    elif calendar:
        pool, source = calendar, "calendar"          # nothing started yet
    else:
        pool, source = [], "folder"

    # Started-but-empty folders first: they're the earliest gaps in the run.
    started_empty = [d for d in subdirs if d not in done]
    missing = [t for t in started_empty if t in pool]
    missing += [t for t in pool if t not in done and t not in missing]

    # What switching to the calendar would add on top — never applied here.
    extra = [t for t in calendar if t not in pool and t not in done] if source != "calendar" else []

    return {
        "run_dir":          str(run_dir),
        "name":             run_dir.name,
        "trade_date":       trade_date,
        "earnings_date":    earnings_date,
        "universe_source":  source,
        "total":            len(pool),
        "done":             done,
        "missing":          missing,
        "calendar_extra":   extra,
        "has_allocation":   (run_dir / "allocation.md").exists(),
        "has_table":        (run_dir / "screening_table.md").exists(),
        "metadata":         meta,
    }


def resumable_runs(reports_root: str | Path | None = None, limit: int = 25) -> list[dict]:
    """Recent runs that are missing tickers or an allocation, newest first."""
    from tradingagents.reports_layout import iter_run_dirs

    out = []
    for d in iter_run_dirs(reports_root or "reports")[:limit]:
        try:
            plan = plan_resume(d)
        except Exception:
            continue
        if plan["missing"] or not plan["has_allocation"]:
            out.append({k: plan[k] for k in
                        ("run_dir", "name", "trade_date", "earnings_date", "total",
                         "has_allocation", "has_table", "universe_source")}
                       | {"calendar_extra": len(plan["calendar_extra"])}
                       | {"done": len(plan["done"]), "missing": len(plan["missing"]),
                          "missing_tickers": plan["missing"][:40]})
    return out


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #

def build_config(meta: dict, overrides: dict | None = None) -> dict:
    """Rebuild the run's LLM config so resumed tickers match the originals."""
    from tradingagents.default_config import DEFAULT_CONFIG

    config = DEFAULT_CONFIG.copy()
    if meta.get("provider"):
        config["llm_provider"] = meta["provider"]
    if meta.get("quick_model"):
        config["quick_think_llm"] = meta["quick_model"]
    if meta.get("deep_model"):
        config["deep_think_llm"] = meta["deep_model"]
    depth = meta.get("depth")
    if isinstance(depth, int):
        config["max_debate_rounds"] = depth
        config["max_risk_discuss_rounds"] = depth
    config["backend_url"] = None
    config.update(overrides or {})
    return config


def resume_run(
    run_dir: str | Path,
    *,
    budget: int = DEFAULT_BUDGET,
    workers: int = DEFAULT_WORKERS,
    allocate: bool = True,
    universe: str = "auto",
    config_overrides: dict | None = None,
    log=print,
    job_id: str | None = None,
    reports_root: str | Path | None = None,
) -> dict:
    """Screen the missing tickers, rebuild the table, run allocation.

    Registers itself in the job registry so the dashboard shows it whatever
    process it runs in, and always deregisters — including on failure.
    """
    from cli import jobs_registry
    from cli.commands.common import _fetch_sector, gather_api_keys, parse_brief_scores
    from cli.commands.screen import run_allocation, screen_ticker
    from tradingagents.allocation.layer import parse_allocation
    from tradingagents.screening_table import write_screening_table

    run_dir = Path(run_dir)
    plan = plan_resume(run_dir, universe=universe)
    missing = plan["missing"]
    config = build_config(plan["metadata"], config_overrides)
    api_keys = gather_api_keys(config["llm_provider"])

    record = jobs_registry.register(
        "resume", job_id=job_id, run_dir=str(run_dir), total=plan["total"],
        label=f"Resume {run_dir.name}", reports_root=reports_root,
        params={"budget": budget, "workers": workers, "missing": len(missing)},
    )
    rid = record["id"]

    summary = {"job_id": rid, "run_dir": str(run_dir), "attempted": len(missing),
               "screened": 0, "errors": 0, "allocated": False}
    try:
        log(f"Resuming {run_dir.name}")
        log(f"  {len(plan['done'])} of {plan['total']} done ({plan['universe_source']} universe) "
            f"→ {len(missing)} to screen")
        log(f"  {config['llm_provider']} · {config['quick_think_llm']} / {config['deep_think_llm']} "
            f"· {len(api_keys) or 1} key(s) · {workers} worker(s)")

        if missing and not api_keys:
            raise RuntimeError(f"no API keys configured for provider {config['llm_provider']}")

        def process(item):
            idx, ticker = item
            # Cooperative stop: a queued ticker is never started. One already in
            # screen_ticker finishes — a thread cannot be interrupted.
            if jobs_registry.is_cancelled(rid, reports_root=reports_root):
                return {"ticker": ticker, "signal": "SKIPPED", "one_liner": "stop requested"}
            wcfg = config.copy()
            if api_keys:
                wcfg["api_key"] = api_keys[idx % len(api_keys)]
            # screen_ticker never raises; guard anyway so one bad ticker can't kill the pool
            try:
                return screen_ticker(ticker, plan["trade_date"], run_dir / ticker, wcfg)
            except Exception as exc:
                return {"ticker": ticker, "signal": "ERROR", "one_liner": str(exc)}

        if missing:
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                futs = [pool.submit(process, it) for it in enumerate(missing)]
                for fut in as_completed(futs):
                    res = fut.result()
                    if res.get("signal") == "SKIPPED":
                        summary["skipped"] = summary.get("skipped", 0) + 1
                    elif res.get("signal") == "ERROR":
                        summary["errors"] += 1
                        log(f"  {res['ticker']:<6} ERROR  {res.get('one_liner', '')[:70]}")
                    else:
                        summary["screened"] += 1
                        log(f"  {res['ticker']:<6} {res.get('signal', '?'):<5} "
                            f"{res.get('total_score', 0):+d}  "
                            f"[{summary['screened'] + summary['errors']}/{len(missing)}]")
        else:
            log("  nothing missing — going straight to the table")

        results = []
        for td in sorted(d for d in run_dir.iterdir() if d.is_dir()):
            r = parse_brief_scores(td)
            if r is None:
                continue
            r["sector"] = _fetch_sector(r["ticker"])
            results.append(r)
        cancelled = jobs_registry.is_cancelled(rid, reports_root=reports_root)
        if not results:
            # Stopped before anything finished is a cancellation, not a failure —
            # raising here would file it as ERROR and hide why it stopped.
            if cancelled:
                log("Stopped by request — nothing screened yet, table not rebuilt.")
                jobs_registry.finish(rid, jobs_registry.CANCELLED, reports_root=reports_root)
                summary["cancelled"] = True
                return summary
            raise RuntimeError("no parseable briefs in the run folder")

        results.sort(key=lambda r: r.get("total_score", 0), reverse=True)
        write_screening_table(results, run_dir / "screening_table.md",
                              f"# Earnings Screener — {plan['trade_date']}")
        log(f"  screening_table.md rebuilt ({len(results)} tickers)")
        summary["table_rows"] = len(results)

        if cancelled:
            # The table above is still worth writing — `resume` reads it back —
            # but a deliberately partial run does not get an 11-call council.
            log(f"Stopped by request — {summary['screened']} screened, "
                f"{summary.get('skipped', 0)} skipped, allocation not run.")
            jobs_registry.finish(rid, jobs_registry.CANCELLED, reports_root=reports_root)
            summary["cancelled"] = True
            return summary

        if allocate:
            log(f"Running allocation council — budget ${budget:,}")
            report = run_allocation(results, plan["trade_date"], run_dir, budget, config,
                                    progress=lambda m: log(f"  {m}"))
            if report:
                data = parse_allocation(report)
                summary["allocated"] = True
                summary["deployed"] = data.get("total_deployed", 0)
                log(f"  deployed ${summary['deployed']:,} across {len(data.get('allocations', []))} rows")

        jobs_registry.finish(rid, jobs_registry.DONE, reports_root=reports_root)
        log("Resume complete.")
        return summary
    except Exception as exc:
        jobs_registry.finish(rid, jobs_registry.ERROR, reports_root=reports_root, detail=str(exc))
        log(f"Resume failed: {exc}")
        raise


# --------------------------------------------------------------------------- #
# CLI command
# --------------------------------------------------------------------------- #

def resume(
    dir: Optional[str] = typer.Option(None, "--dir", "-d", help="Run folder to resume (skips the picker)"),
    budget: int = typer.Option(DEFAULT_BUDGET, "--budget", help="Capital budget for allocation ($)"),
    workers: int = typer.Option(DEFAULT_WORKERS, "--workers", "-w", help="Parallel screening workers"),
    no_allocate: bool = typer.Option(False, "--no-allocate", help="Screen the gaps but skip allocation"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation (for cron)"),
    universe: str = typer.Option("auto", "--universe",
                                 help="auto = what the run submitted; calendar = every ticker on that earnings date"),
):
    """Finish an interrupted screening run: screen what's missing, then allocate."""
    import questionary
    from rich.console import Console

    console = Console()

    if dir:
        run_dir = Path(dir)
        if not run_dir.exists():
            console.print(f"[red]Directory not found: {run_dir}[/red]")
            raise typer.Exit(1)
    else:
        candidates = resumable_runs()
        if not candidates:
            console.print("[green]Nothing to resume — every recent run is complete.[/green]")
            raise typer.Exit(0)
        console.print("\n[bold]Resumable runs:[/bold]\n")
        for i, c in enumerate(candidates, 1):
            gap = f"[yellow]{c['missing']} missing[/yellow]" if c["missing"] else "[dim]all screened[/dim]"
            alloc = "" if c["has_allocation"] else " [magenta]· no allocation[/magenta]"
            console.print(f"  [cyan]{i}.[/cyan] {c['name']}  [dim]{c['done']}/{c['total']}[/dim]  {gap}{alloc}")
        console.print()
        choice = questionary.text("Enter number:").ask()
        if not choice:
            raise typer.Exit(0)
        try:
            run_dir = Path(candidates[int(choice.strip()) - 1]["run_dir"])
        except (ValueError, IndexError):
            console.print("[red]Invalid selection.[/red]")
            raise typer.Exit(1)

    plan = plan_resume(run_dir, universe=universe)
    console.print(f"\n[bold]{plan['name']}[/bold]")
    console.print(f"  complete : {len(plan['done'])}/{plan['total']} ({plan['universe_source']} universe)")
    console.print(f"  to screen: {len(plan['missing'])}")
    if plan["missing"]:
        preview = ", ".join(plan["missing"][:20]) + ("…" if len(plan["missing"]) > 20 else "")
        console.print(f"  [dim]{preview}[/dim]")
    console.print(f"  allocation: {'exists (will be rewritten)' if plan['has_allocation'] else 'missing'}")
    if plan["calendar_extra"]:
        console.print(f"  [dim]{len(plan['calendar_extra'])} more ticker(s) reported that day but were not in this run — "
                      f"add with --universe calendar[/dim]")
    console.print()

    if not yes:
        est = len(plan["missing"]) * 20 / max(1, workers)
        if not questionary.confirm(
            f"Screen {len(plan['missing'])} ticker(s) with {workers} workers "
            f"(~{est:.0f} min) and {'skip' if no_allocate else 'run'} allocation?",
            default=True,
        ).ask():
            raise typer.Exit(0)

    summary = resume_run(run_dir, budget=budget, workers=workers, allocate=not no_allocate,
                         universe=universe,
                         log=lambda m: console.print(f"[dim]{m}[/dim]"))
    console.print(f"\n[green]✓ {summary['screened']} screened, {summary['errors']} errored"
                  f"{', allocation written' if summary['allocated'] else ''}[/green]")
