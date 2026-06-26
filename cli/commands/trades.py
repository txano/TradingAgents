"""Trades, calibrate, correlation, and stats commands."""

import math
from pathlib import Path
from typing import Optional

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

console = Console()


def trades():
    """Display your full trade history from trades.json."""
    import json as _json

    from cli.commands.common import _trades_path
    trade_log_path = _trades_path()
    if not trade_log_path.exists():
        console.print("[yellow]No trades.json found. Log a trade with 'uv run tradingagents reflect'.[/yellow]")
        return

    try:
        all_trades = _json.loads(trade_log_path.read_text(encoding="utf-8"))
    except Exception as e:
        console.print(f"[red]Error reading trades.json: {e}[/red]")
        return

    if not all_trades:
        console.print("[yellow]No trades logged yet.[/yellow]")
        return

    console.print()
    console.print(Rule("[bold cyan]Trade History[/bold cyan]"))
    console.print()

    tbl = Table(
        box=box.ROUNDED,
        title=f"[bold]All Trades ({len(all_trades)})[/bold]",
        show_lines=True,
    )
    tbl.add_column("#", justify="right", style="dim", width=3)
    tbl.add_column("Ticker", style="cyan bold", width=8)
    tbl.add_column("Sector", width=14)
    tbl.add_column("Direction", justify="center", width=10)
    tbl.add_column("Shares", justify="right", width=8)
    tbl.add_column("Entry", justify="right", width=9)
    tbl.add_column("Exit", justify="right", width=9)
    tbl.add_column("P&L $", justify="right", width=11)
    tbl.add_column("P&L %", justify="right", width=8)
    tbl.add_column("Outcome", justify="center", width=10)
    tbl.add_column("Trade Date", width=12)

    total_pnl = 0.0
    wins = losses = 0

    for i, t in enumerate(all_trades, 1):
        pnl = t.get("pnl", 0.0)
        pnl_pct = t.get("pnl_pct", 0.0)
        outcome = t.get("outcome", "?")
        direction = t.get("direction", "?")
        total_pnl += pnl
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
        pnl_color = "green" if pnl >= 0 else "red"
        dir_color = {"BUY": "green", "SHORT": "red"}.get(direction, "white")
        out_color = {"WIN": "green", "LOSS": "red", "BREAK_EVEN": "yellow"}.get(outcome, "white")
        tbl.add_row(
            str(i),
            t.get("ticker", "?"),
            t.get("sector", "—"),
            f"[{dir_color}]{direction}[/{dir_color}]",
            f"{t.get('shares', 0):.0f}",
            f"${t.get('entry_price', 0):.2f}",
            f"${t.get('exit_price', 0):.2f}",
            f"[{pnl_color}]${pnl:+.2f}[/{pnl_color}]",
            f"[{pnl_color}]{pnl_pct:+.1f}%[/{pnl_color}]",
            f"[{out_color}]{outcome}[/{out_color}]",
            t.get("trade_date", "?"),
        )

    console.print(tbl)

    n = len(all_trades)
    win_rate = wins / n * 100 if n > 0 else 0
    avg_pnl = total_pnl / n if n > 0 else 0
    pnl_color = "green" if total_pnl >= 0 else "red"
    console.print(
        f"\n  [bold]Total:[/bold] {n} trade(s)  |  "
        f"Win rate: [cyan]{win_rate:.0f}%[/cyan] ({wins}W / {losses}L)  |  "
        f"Total P&L: [{pnl_color}]${total_pnl:+.2f}[/{pnl_color}]  |  "
        f"Avg per trade: [{pnl_color}]${avg_pnl:+.2f}[/{pnl_color}]"
    )
    console.print()


def calibrate():
    """Calibrate screening predictions against actual earnings outcomes."""
    from tradingagents.calibration import calibrate_screening_run

    from tradingagents.reports_layout import iter_run_dirs

    reports_dir = Path("reports")
    if not reports_dir.exists():
        console.print("[yellow]No reports/ directory found. Run 'screen' first.[/yellow]")
        return

    all_runs = iter_run_dirs(reports_dir)
    if not all_runs:
        console.print("[yellow]No screening runs found in reports/earnings/.[/yellow]")
        return

    console.print()
    console.print(Rule("[bold cyan]Calibration[/bold cyan]"))
    console.print()
    console.print("[bold]Available screening runs:[/bold]\n")

    for i, d in enumerate(all_runs, 1):
        has_table = (d / "screening_table.md").exists()
        has_cal = (d / "calibration.json").exists()
        status = (
            "[green]calibrated[/green]" if has_cal
            else ("[yellow]not calibrated[/yellow]" if has_table else "[dim]no table[/dim]")
        )
        console.print(f"  [cyan]{i}.[/cyan] {d.name}  {status}")

    console.print("  [dim]0. Cancel[/dim]\n")
    console.print("[dim]Enter a number to calibrate that run, or 0 to cancel.[/dim]")

    while True:
        choice = typer.prompt("").strip()
        try:
            n = int(choice)
            if n == 0:
                return
            if 1 <= n <= len(all_runs):
                selected = all_runs[n - 1]
                break
            console.print(f"[red]Enter 0–{len(all_runs)}[/red]")
        except ValueError:
            console.print("[red]Enter a number[/red]")

    if not (selected / "screening_table.md").exists():
        console.print(f"[red]No screening_table.md in {selected.name}. Cannot calibrate.[/red]")
        return

    console.print(f"\n[bold]Calibrating:[/bold] {selected.name}")
    console.print("[dim]Fetching actual earnings data from yfinance...[/dim]\n")

    try:
        with console.status("[bold yellow]Fetching actuals and computing accuracy...[/bold yellow]"):
            result = calibrate_screening_run(selected)
    except Exception as exc:
        console.print(f"[red]Calibration error: {exc}[/red]")
        return

    ba = result.get("beat_accuracy_pct")
    sa = result.get("signal_accuracy_pct")
    n_tickers = result.get("tickers", 0)

    console.print(Panel(
        f"[bold]Run:[/bold] {selected.name}\n"
        f"[bold]Tickers:[/bold] {n_tickers}\n\n"
        f"[bold]Beat prediction accuracy:[/bold] "
        f"{'[green]' + str(ba) + '%[/green]' if ba is not None else '[dim]N/A[/dim]'}\n"
        f"[bold]Signal accuracy:[/bold] "
        f"{'[green]' + str(sa) + '%[/green]' if sa is not None else '[dim]N/A[/dim]'}",
        title="[bold green]Calibration Results[/bold green]",
        border_style="green",
        padding=(1, 2),
    ))

    cal_tbl = Table(box=box.ROUNDED, title="[bold]Per-Ticker Results[/bold]", show_lines=True)
    cal_tbl.add_column("Ticker", style="cyan bold", width=8)
    cal_tbl.add_column("Earnings", width=12)
    cal_tbl.add_column("Beat", justify="center", width=6)
    cal_tbl.add_column("Guid.", justify="center", width=6)
    cal_tbl.add_column("Setup", justify="center", width=6)
    cal_tbl.add_column("Total", justify="center", width=7)
    cal_tbl.add_column("Conf.", justify="center", width=7)
    cal_tbl.add_column("Signal", justify="center", width=8)
    cal_tbl.add_column("Actual", justify="center", width=8)
    cal_tbl.add_column("Surprise%", justify="center", width=10)
    cal_tbl.add_column("Price Δ%", justify="center", width=10)
    cal_tbl.add_column("Beat✓", justify="center", width=7)
    cal_tbl.add_column("Signal✓", justify="center", width=8)

    def _sc(n: int) -> str:
        color = "green" if n > 0 else ("red" if n < 0 else "dim")
        return f"[{color}]{n:+d}[/{color}]"

    for row in result.get("rows", []):
        b_sym = "[green]✓[/green]" if row["beat_prediction_correct"] else ("[red]✗[/red]" if row["beat_prediction_correct"] is False else "[dim]?[/dim]")
        s_sym = "[green]✓[/green]" if row["signal_correct"] else ("[red]✗[/red]" if row["signal_correct"] is False else "[dim]N/A[/dim]")
        surp = f"{row['surprise_pct']:+.1f}%" if row["surprise_pct"] is not None else "?"
        pc = f"{row['price_change_pct']:+.1f}%" if row["price_change_pct"] is not None else "?"
        act = "Beat" if row["actual_beat"] else ("Miss" if row["actual_beat"] is False else "?")
        cal_tbl.add_row(
            row["ticker"], row["earnings_date"],
            _sc(row["beat_score"]), _sc(row["guidance_score"]),
            _sc(row["setup_score"]), _sc(row["total_score"]),
            row["confidence"], row["signal"],
            act, surp, pc, b_sym, s_sym,
        )

    console.print(cal_tbl)
    console.print(f"\n[green]✓ Saved:[/green] {selected / 'calibration.json'}")
    console.print(f"[green]✓ Saved:[/green] {selected / 'calibration.md'}")
    console.print(f"[green]✓ Updated:[/green] {Path('reports') / 'calibration_master.json'}")
    console.print(f"[green]✓ Updated:[/green] {Path('reports') / 'calibration_master.md'}\n")

    from cli.commands.reports import _auto_build_web
    _auto_build_web()


def correlation():
    """Correlate beat / guidance / setup scores against actual trade outcomes.

    Loads all calibration data, computes Pearson r for each score bucket vs
    signal-direction accuracy and directional price move, shows per-score-value
    accuracy tables, and suggests updated allocation weights proportional to
    each bucket's predictive power.
    """
    import json as _json
    import pandas as _pd
    from tradingagents.calibration import load_all_calibrations
    from tradingagents.allocation.weights import load_weights, save_weights

    reports_dir = Path("reports")

    console.print()
    console.print(Rule("[bold cyan]Score Correlation Analysis[/bold cyan]"))
    console.print()

    calibrations = load_all_calibrations(reports_dir) if reports_dir.exists() else []
    all_rows = [row for cal in calibrations for row in cal.get("rows", [])]

    if not all_rows:
        console.print(
            "[yellow]No calibration data found. "
            "Run 'tradingagents calibrate' after earnings are announced.[/yellow]"
        )
        return

    df = _pd.DataFrame(all_rows)

    for col in ["beat_score", "guidance_score", "setup_score", "total_score", "price_change_pct"]:
        if col in df.columns:
            df[col] = _pd.to_numeric(df[col], errors="coerce")
        else:
            df[col] = float("nan")

    df["signal_bin"] = df["signal_correct"].map({True: 1.0, False: 0.0})

    def _dir_change(row):
        pct = row["price_change_pct"]
        if _pd.isna(pct):
            return float("nan")
        sig = row.get("signal", "")
        return pct if sig == "BUY" else (-pct if sig == "SHORT" else float("nan"))

    df["dir_change"] = df.apply(_dir_change, axis=1)

    n_total     = len(df)
    n_signal    = int(df["signal_bin"].notna().sum())
    n_price     = int(df["price_change_pct"].notna().sum())
    n_screening = len(calibrations)

    console.print(Panel(
        f"[bold]Screening runs:[/bold] {n_screening}  |  "
        f"[bold]Tickers screened:[/bold] {n_total}\n"
        f"[bold]With signal outcome:[/bold] {n_signal}  |  "
        f"[bold]With price data:[/bold] {n_price}",
        title="[bold cyan]Dataset[/bold cyan]",
        border_style="cyan",
        padding=(0, 2),
    ))
    console.print()

    if n_signal < 3:
        console.print(
            "[yellow]Not enough resolved signals for correlation analysis (need ≥ 3).[/yellow]\n"
            "[dim]Run 'tradingagents calibrate' on more past screening runs.[/dim]"
        )
        return

    def _pearson_r(s_x: "_pd.Series", s_y: "_pd.Series"):
        valid = s_x.notna() & s_y.notna()
        n = int(valid.sum())
        if n < 3:
            return None, n
        import numpy as _np
        x, y = s_x[valid].values.astype(float), s_y[valid].values.astype(float)
        mx, my = x.mean(), y.mean()
        xd, yd = x - mx, y - my
        denom = float((xd**2).sum()**0.5 * (yd**2).sum()**0.5)
        if denom == 0:
            return None, n
        return float((xd * yd).sum() / denom), n

    score_cols = [c for c in ["beat_score", "guidance_score", "setup_score"]
                  if c in df.columns and int(df[c].notna().sum()) >= 3]

    def _r_color(r):
        if r is None:
            return "[dim]N/A[/dim]"
        ar = abs(r)
        c = "green" if ar >= 0.4 else ("yellow" if ar >= 0.2 else "red")
        return f"[{c}]{r:+.3f}[/{c}]"

    def _strength(r):
        if r is None:
            return "[dim]—[/dim]"
        ar = abs(r)
        if ar >= 0.5:
            return "[green]Strong[/green]"
        if ar >= 0.3:
            return "[yellow]Moderate[/yellow]"
        if ar >= 0.1:
            return "[dim]Weak[/dim]"
        return "[red]Negligible[/red]"

    corr_tbl = Table(
        box=box.ROUNDED,
        title="[bold]Score Correlations with Trade Outcomes[/bold]",
        show_lines=True,
    )
    corr_tbl.add_column("Bucket",       style="cyan bold", width=14)
    corr_tbl.add_column("r  signal ✓",  justify="center",  width=14)
    corr_tbl.add_column("r  dir.move",  justify="center",  width=14)
    corr_tbl.add_column("N (signal)",   justify="right",   width=11)
    corr_tbl.add_column("Strength",     width=14)

    abs_corrs: dict = {}
    for col in score_cols:
        r_sig, n_sig = _pearson_r(df[col], df["signal_bin"])
        r_prc, _     = _pearson_r(df[col], df["dir_change"])
        if r_sig is not None:
            abs_corrs[col] = abs(r_sig)
        corr_tbl.add_row(
            col.replace("_score", "").capitalize(),
            _r_color(r_sig),
            _r_color(r_prc),
            str(n_sig),
            _strength(r_sig),
        )

    console.print(corr_tbl)
    console.print(
        "[dim]r = Pearson r.  signal ✓ = 1 when predicted direction matched price move.  "
        "dir.move = price change in signal direction (BUY→+, SHORT→−).[/dim]\n"
    )

    resolved = df[df["signal_bin"].notna()].copy()
    for col in score_cols:
        bucket_name = col.replace("_score", "").capitalize()
        grp = (
            resolved.groupby(col)["signal_bin"]
            .agg(["sum", "count"])
            .rename(columns={"sum": "correct", "count": "total"})
            .reset_index()
        )
        if grp.empty:
            continue

        bkt_tbl = Table(
            box=box.ROUNDED,
            title=f"[bold]{bucket_name} Score — Signal Accuracy by Value[/bold]",
            show_lines=True,
        )
        bkt_tbl.add_column("Score",          justify="center", style="cyan", width=8)
        bkt_tbl.add_column("Signals",         justify="right",               width=9)
        bkt_tbl.add_column("Correct",         justify="right",               width=9)
        bkt_tbl.add_column("Accuracy",        justify="center",              width=10)
        bkt_tbl.add_column("Avg Dir Move %",  justify="right",               width=15)

        for _, row in grp.sort_values(col).iterrows():
            score_val = int(row[col])
            correct   = int(row["correct"])
            total     = int(row["total"])
            acc       = row["correct"] / row["total"] * 100 if row["total"] > 0 else 0
            sub_price = resolved[resolved[col] == score_val]["dir_change"].dropna()
            avg_move  = float(sub_price.mean()) if len(sub_price) > 0 else float("nan")

            acc_color = "green" if acc >= 60 else ("yellow" if acc >= 40 else "red")
            move_str  = (
                f"[{'green' if avg_move > 0 else 'red'}]{avg_move:+.1f}%[/{'green' if avg_move > 0 else 'red'}]"
                if not math.isnan(avg_move) else "[dim]N/A[/dim]"
            )
            bkt_tbl.add_row(
                f"{score_val:+d}",
                str(total),
                str(correct),
                f"[{acc_color}]{acc:.0f}%[/{acc_color}]",
                move_str,
            )
        console.print(bkt_tbl)

    if len(score_cols) >= 2:
        ic_tbl = Table(
            box=box.ROUNDED,
            title="[bold]Score Intercorrelations[/bold]",
            show_lines=True,
        )
        ic_tbl.add_column("", style="cyan bold", width=14)
        for col in score_cols:
            ic_tbl.add_column(col.replace("_score", "").capitalize(), justify="center", width=12)

        for col_a in score_cols:
            row_vals = [col_a.replace("_score", "").capitalize()]
            for col_b in score_cols:
                if col_a == col_b:
                    row_vals.append("[dim]—[/dim]")
                else:
                    r, _ = _pearson_r(df[col_a], df[col_b])
                    row_vals.append(_r_color(r))
            ic_tbl.add_row(*row_vals)

        console.print(ic_tbl)
        console.print(
            "[dim]High intercorrelation (|r| > 0.7) = two buckets carry similar information. "
            "Consider down-weighting the less predictive one.[/dim]\n"
        )

    current_weights = load_weights()
    bucket_key_map  = {"beat_score": "beat", "guidance_score": "guidance", "setup_score": "setup"}

    if abs_corrs:
        total_r  = sum(abs_corrs.values()) or 1.0
        n_b      = len(abs_corrs)
        suggested = {
            bucket_key_map[k]: round(v / total_r * n_b, 2)
            for k, v in abs_corrs.items()
        }
        for k in ("beat", "guidance", "setup"):
            if k not in suggested:
                suggested[k] = current_weights.get(k, 1.0)

        wt_tbl = Table(
            box=box.ROUNDED,
            title="[bold]Suggested Allocation Weights[/bold]",
            show_lines=True,
        )
        wt_tbl.add_column("Bucket",    style="cyan bold", width=14)
        wt_tbl.add_column("|r|",       justify="center",  width=8)
        wt_tbl.add_column("Current",   justify="center",  width=10)
        wt_tbl.add_column("Suggested", justify="center",  width=12)
        wt_tbl.add_column("Change",    justify="center",  width=12)

        apply_args = []
        for col in score_cols:
            key   = bucket_key_map[col]
            ar    = abs_corrs.get(col, 0.0)
            cur   = current_weights.get(key, 1.0)
            sug   = suggested[key]
            delta = sug - cur
            if delta > 0.05:
                delta_str = f"[green]+{delta:.2f}[/green]"
            elif delta < -0.05:
                delta_str = f"[red]{delta:.2f}[/red]"
            else:
                delta_str = "[dim]≈[/dim]"
            wt_tbl.add_row(
                key.capitalize(),
                f"{ar:.3f}",
                f"{cur:.2f}",
                f"[bold]{sug:.2f}[/bold]",
                delta_str,
            )
            apply_args.append((key, sug))

        console.print(wt_tbl)
        console.print(
            "[dim]Suggested weights are proportional to |r| with signal correctness, "
            "normalized so their average equals 1.0.[/dim]\n"
        )

        apply = typer.prompt("Apply suggested weights? [y/N]", default="N").strip().upper()
        if apply in ("Y", "YES"):
            beat_w  = suggested.get("beat",     current_weights["beat"])
            guid_w  = suggested.get("guidance", current_weights["guidance"])
            setup_w = suggested.get("setup",    current_weights["setup"])
            fund_w  = current_weights.get("fundamentals", 1.5)
            save_weights(beat_w, guid_w, setup_w, fund_w)
            console.print(
                f"[green]✓ Weights updated:[/green] "
                f"beat=[cyan]{beat_w:.2f}[/cyan]  "
                f"guidance=[cyan]{guid_w:.2f}[/cyan]  "
                f"setup=[cyan]{setup_w:.2f}[/cyan]"
            )
        else:
            manual_cmd = "tradingagents allocation-weights " + " ".join(
                f"--{k} {v:.2f}" for k, v in apply_args
            )
            console.print(f"[dim]To apply manually: {manual_cmd}[/dim]")
    else:
        console.print("[dim]Not enough data to suggest weights.[/dim]")

    console.print()


def stats():
    """Display accuracy statistics from trade history and calibration results."""
    import json as _json
    import yfinance as _yf
    import datetime as _dt
    from tradingagents.calibration import load_all_calibrations

    console.print()
    console.print(Rule("[bold cyan]TradingAgents Statistics[/bold cyan]"))
    console.print()

    from cli.commands.common import _trades_path
    trade_log_path = _trades_path()
    all_trades = []
    if trade_log_path.exists():
        try:
            all_trades = _json.loads(trade_log_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    if all_trades:
        n = len(all_trades)
        wins = sum(1 for t in all_trades if t.get("pnl", 0) > 0)
        losses = sum(1 for t in all_trades if t.get("pnl", 0) < 0)
        total_pnl = sum(t.get("pnl", 0) for t in all_trades)
        win_rate = wins / n * 100 if n else 0

        beat_data = [t for t in all_trades if t.get("beat_prediction_correct") is not None]
        beat_acc = sum(1 for t in beat_data if t.get("beat_prediction_correct")) / len(beat_data) * 100 if beat_data else None

        guid_data = [t for t in all_trades if t.get("guidance_prediction_correct") is not None]
        guid_acc = sum(1 for t in guid_data if t.get("guidance_prediction_correct")) / len(guid_data) * 100 if guid_data else None

        pnl_color = "green" if total_pnl >= 0 else "red"

        console.print(Panel(
            f"[bold]Trades:[/bold] {n}  |  [bold]Wins:[/bold] {wins}  |  [bold]Losses:[/bold] {losses}\n"
            f"[bold]Win rate:[/bold] [cyan]{win_rate:.0f}%[/cyan]  |  "
            f"[bold]Total P&L:[/bold] [{pnl_color}]${total_pnl:+.2f}[/{pnl_color}]\n"
            f"[bold]Beat prediction accuracy:[/bold] "
            f"{'[green]' + f'{beat_acc:.0f}%[/green]  (' + str(len(beat_data)) + ' trades)' if beat_acc is not None else '[dim]N/A (no reflection data)[/dim]'}\n"
            f"[bold]Guidance prediction accuracy:[/bold] "
            f"{'[green]' + f'{guid_acc:.0f}%[/green]  (' + str(len(guid_data)) + ' trades)' if guid_acc is not None else '[dim]N/A (no reflection data)[/dim]'}",
            title="[bold cyan]Trade History[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        ))

        for direction in ("BUY", "SHORT"):
            dir_trades = [t for t in all_trades if t.get("direction") == direction]
            if dir_trades:
                dw = sum(1 for t in dir_trades if t.get("pnl", 0) > 0)
                dr = dw / len(dir_trades) * 100
                dir_color = "green" if direction == "BUY" else "red"
                console.print(f"  [{dir_color}]{direction}[/{dir_color}]: {len(dir_trades)} trade(s), win rate [cyan]{dr:.0f}%[/cyan]")
        console.print()
    else:
        console.print("[dim]No trades logged yet. Run 'uv run tradingagents reflect' after closing a trade.[/dim]\n")

    reports_dir = Path("reports")
    calibrations = load_all_calibrations(reports_dir) if reports_dir.exists() else []

    if calibrations:
        all_rows = [row for cal in calibrations for row in cal.get("rows", [])]

        with_beat = [r for r in all_rows if r["beat_prediction_correct"] is not None]
        with_signal = [r for r in all_rows if r["signal_correct"] is not None]

        beat_acc_cal = sum(1 for r in with_beat if r["beat_prediction_correct"]) / len(with_beat) * 100 if with_beat else None
        sig_acc_cal = sum(1 for r in with_signal if r["signal_correct"]) / len(with_signal) * 100 if with_signal else None

        conf_stats = {}
        for r in with_signal:
            c = r.get("confidence", "?")
            if c not in conf_stats:
                conf_stats[c] = {"correct": 0, "total": 0}
            conf_stats[c]["total"] += 1
            if r["signal_correct"]:
                conf_stats[c]["correct"] += 1

        bucket_stats: dict = {}
        for r in with_signal:
            ts = r.get("total_score", 0)
            if ts >= 8:
                bucket = "≥+8 (strong)"
            elif ts >= 4:
                bucket = "+4 to +7"
            elif ts >= 0:
                bucket = "0 to +3"
            elif ts >= -4:
                bucket = "-1 to -4"
            else:
                bucket = "≤-5 (bearish)"
            if bucket not in bucket_stats:
                bucket_stats[bucket] = {"correct": 0, "total": 0}
            bucket_stats[bucket]["total"] += 1
            if r["signal_correct"]:
                bucket_stats[bucket]["correct"] += 1

        cal_summary = (
            f"[bold]Runs calibrated:[/bold] {len(calibrations)}  |  "
            f"[bold]Tickers:[/bold] {len(all_rows)}\n"
            f"[bold]Beat prediction accuracy:[/bold] "
            f"{'[green]' + f'{beat_acc_cal:.0f}%[/green]  (' + str(len(with_beat)) + ' tickers)' if beat_acc_cal is not None else '[dim]N/A[/dim]'}\n"
            f"[bold]Signal accuracy:[/bold] "
            f"{'[green]' + f'{sig_acc_cal:.0f}%[/green]  (' + str(len(with_signal)) + ' tickers)' if sig_acc_cal is not None else '[dim]N/A[/dim]'}"
        )
        console.print(Panel(cal_summary, title="[bold magenta]Calibration (Screening Accuracy)[/bold magenta]", border_style="magenta", padding=(1, 2)))

        if conf_stats:
            conf_tbl = Table(box=box.ROUNDED, title="[bold]Signal Accuracy by Confidence[/bold]", show_lines=True)
            conf_tbl.add_column("Confidence", style="cyan", width=12)
            conf_tbl.add_column("Signals", justify="right", width=9)
            conf_tbl.add_column("Correct", justify="right", width=9)
            conf_tbl.add_column("Accuracy", justify="center", width=10)
            for conf, s in sorted(conf_stats.items()):
                acc = s["correct"] / s["total"] * 100 if s["total"] else 0
                acc_color = "green" if acc >= 60 else ("yellow" if acc >= 40 else "red")
                conf_tbl.add_row(conf, str(s["total"]), str(s["correct"]), f"[{acc_color}]{acc:.0f}%[/{acc_color}]")
            console.print(conf_tbl)

        if bucket_stats:
            bucket_tbl = Table(box=box.ROUNDED, title="[bold]Signal Accuracy by Total Score Bucket[/bold]", show_lines=True)
            bucket_tbl.add_column("Score Bucket", style="cyan", width=18)
            bucket_tbl.add_column("Signals", justify="right", width=9)
            bucket_tbl.add_column("Correct", justify="right", width=9)
            bucket_tbl.add_column("Accuracy", justify="center", width=10)
            bucket_order = ["≥+8 (strong)", "+4 to +7", "0 to +3", "-1 to -4", "≤-5 (bearish)"]
            for bucket in bucket_order:
                if bucket in bucket_stats:
                    s = bucket_stats[bucket]
                    acc = s["correct"] / s["total"] * 100 if s["total"] else 0
                    acc_color = "green" if acc >= 60 else ("yellow" if acc >= 40 else "red")
                    bucket_tbl.add_row(bucket, str(s["total"]), str(s["correct"]), f"[{acc_color}]{acc:.0f}%[/{acc_color}]")
            console.print(bucket_tbl)
    else:
        console.print("[dim]No calibration data yet. Run 'uv run tradingagents calibrate' after earnings are announced.[/dim]\n")

    if not all_trades:
        return

    console.print(Rule("[bold cyan]Capital & Benchmark Comparison[/bold cyan]"))
    console.print()

    _groups: dict = {}
    for t in all_trades:
        key = (t.get("ticker", ""), t.get("exit_date", ""))
        sh  = t.get("shares", 0) or 0
        ep  = t.get("entry_price", 0) or 0
        if key not in _groups:
            _groups[key] = {"pnl": 0.0, "shares": 0.0, "_entry_wtd": 0.0, "_sh_wtd": 0.0, "exit_date": key[1]}
        g = _groups[key]
        g["pnl"]        += t.get("pnl", 0) or 0
        g["shares"]     += sh
        g["_entry_wtd"] += ep * sh
        g["_sh_wtd"]    += sh

    consolidated = []
    for g in _groups.values():
        avg_ep  = g["_entry_wtd"] / g["_sh_wtd"] if g["_sh_wtd"] else 0
        cost    = avg_ep * g["_sh_wtd"]
        consolidated.append({"exit_date": g["exit_date"], "cost_basis": cost, "pnl": g["pnl"]})

    if not consolidated:
        return

    _daily: dict = {}
    for c in consolidated:
        d = c["exit_date"]
        _daily[d] = _daily.get(d, 0.0) + c["cost_basis"]

    sorted_dates   = sorted(_daily.keys())
    first_date_str = sorted_dates[0]
    last_date_str  = sorted_dates[-1]
    today_str      = _dt.date.today().isoformat()

    total_cost     = sum(c["cost_basis"] for c in consolidated)
    total_pnl_val  = sum(c["pnl"]        for c in consolidated)
    actual_ret_pct = total_pnl_val / total_cost * 100 if total_cost else 0
    avg_daily_cap  = sum(_daily.values()) / len(_daily)

    daily_tbl = Table(box=box.ROUNDED, title="[bold]Capital Deployed by Day[/bold]", show_lines=True)
    daily_tbl.add_column("Exit Date",  style="cyan",    width=13)
    daily_tbl.add_column("Capital",    justify="right", width=14)
    daily_tbl.add_column("Day P&L",    justify="right", width=12)
    daily_tbl.add_column("Day Return", justify="right", width=11)

    daily_pnl_by_date: dict = {}
    for c in consolidated:
        d = c["exit_date"]
        daily_pnl_by_date[d] = daily_pnl_by_date.get(d, 0.0) + c["pnl"]

    for d in sorted_dates:
        cap     = _daily[d]
        day_pnl = daily_pnl_by_date.get(d, 0.0)
        day_ret = day_pnl / cap * 100 if cap else 0
        pnl_c   = "green" if day_pnl >= 0 else "red"
        daily_tbl.add_row(
            d,
            f"${cap:,.0f}",
            f"[{pnl_c}]{'+' if day_pnl >= 0 else ''}{day_pnl:,.0f}[/{pnl_c}]",
            f"[{pnl_c}]{day_ret:+.2f}%[/{pnl_c}]",
        )

    avg_c = "green" if avg_daily_cap >= 0 else "red"
    pnl_c = "green" if total_pnl_val >= 0 else "red"
    daily_tbl.add_section()
    daily_tbl.add_row(
        "[bold]Average / Total[/bold]",
        f"[bold]${avg_daily_cap:,.0f}[/bold] avg",
        f"[{pnl_c}][bold]{'+' if total_pnl_val >= 0 else ''}{total_pnl_val:,.0f}[/bold][/{pnl_c}]",
        f"[{pnl_c}][bold]{actual_ret_pct:+.2f}%[/bold][/{pnl_c}]",
    )
    console.print(daily_tbl)
    console.print(
        f"[dim]Total cost basis across all positions: ${total_cost:,.0f}  |  "
        f"{len(_daily)} active trading days[/dim]\n"
    )

    console.print(f"[dim]Fetching QQQ and SPY prices ({first_date_str} → {today_str})…[/dim]")
    bench_rows = []
    for sym in ("QQQ", "SPY"):
        try:
            df = _yf.download(sym, start=first_date_str, end=today_str, progress=False, auto_adjust=True)
            if df.empty:
                continue
            close = df["Close"].squeeze()
            p0    = float(close.iloc[0])
            p1    = float(close.iloc[-1])
            ret   = (p1 - p0) / p0
            bench_pnl = avg_daily_cap * ret
            bench_rows.append({
                "sym":  sym,
                "p0":   p0,
                "p1":   p1,
                "ret":  ret * 100,
                "pnl":  bench_pnl,
                "diff": total_pnl_val - bench_pnl,
            })
        except Exception:
            pass

    if bench_rows:
        bm_tbl = Table(
            box=box.ROUNDED,
            title=(
                f"[bold]Benchmark: avg daily capital ${avg_daily_cap:,.0f} "
                f"invested on {first_date_str} vs today[/bold]"
            ),
            show_lines=True,
        )
        bm_tbl.add_column("",            style="cyan bold", width=10)
        bm_tbl.add_column("Entry price", justify="right",   width=13)
        bm_tbl.add_column("Today",       justify="right",   width=11)
        bm_tbl.add_column("Return %",    justify="center",  width=11)
        bm_tbl.add_column("P&L $",       justify="right",   width=13)
        bm_tbl.add_column("vs Your P&L", justify="right",   width=16)
        bm_tbl.add_column("Winner",      justify="center",  width=12)

        ret_c = "green" if actual_ret_pct >= 0 else "red"
        bm_tbl.add_row(
            "[bold]You[/bold]",
            "[dim]—[/dim]", "[dim]—[/dim]",
            f"[{ret_c}]{actual_ret_pct:+.2f}%[/{ret_c}]",
            f"[{ret_c}]{'+' if total_pnl_val >= 0 else ''}{total_pnl_val:,.0f}[/{ret_c}]",
            "[dim]—[/dim]",
            "[dim]—[/dim]",
        )

        for br in bench_rows:
            rc      = "green" if br["ret"] >= 0 else "red"
            dc      = "green" if br["diff"] >= 0 else "red"
            winner  = "[green]You ✓[/green]" if br["diff"] > 0 else f"[yellow]{br['sym']}[/yellow]"
            bm_tbl.add_row(
                br["sym"],
                f"${br['p0']:,.2f}",
                f"${br['p1']:,.2f}",
                f"[{rc}]{br['ret']:+.2f}%[/{rc}]",
                f"[{rc}]{'+' if br['pnl'] >= 0 else ''}{br['pnl']:,.0f}[/{rc}]",
                f"[{dc}]{'+' if br['diff'] >= 0 else ''}{br['diff']:,.0f}[/{dc}]",
                winner,
            )

        console.print(bm_tbl)
        console.print(
            f"[dim]Benchmark: ${avg_daily_cap:,.0f} avg daily capital invested on "
            f"{first_date_str} (first exit date) and held until today ({today_str}).\n"
            "Entry dates unavailable for IBKR-imported trades — exit dates used as period proxy.[/dim]\n"
        )
    else:
        console.print("[dim]Could not fetch benchmark prices (check network connection).[/dim]\n")
