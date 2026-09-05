"""File-backed registry of long-running jobs.

The dashboard's in-memory ``_jobs`` dict dies with the server process, so a
restart — or a job started *outside* the server, e.g. a detached CLI run —
leaves the UI claiming nothing is happening while work continues. That is how a
149-ticker screen ran invisibly for hours on 2026-08-10 after a restart killed
its parent.

This module is the durable half. One JSON file every process can write to, with
two properties that make it honest rather than merely persistent:

* **Liveness is derived, not trusted.** A record says "running"; whether it *is*
  running is decided by signalling its recorded PID at read time. A job whose
  process is gone reads back as ``interrupted``, which is exactly the state the
  dashboard needs to offer a resume.
* **Progress is recomputed, not reported.** For a run-folder job, "done" is a
  count of the briefs on disk. A job that never calls back — or that was started
  by a script predating this registry — still shows accurate progress.

Registry entries are advisory: losing the file loses visibility, never work.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

REGISTRY_FILE = "jobs.json"
KEEP_FINISHED_DAYS = 7

# Statuses. `interrupted` is inferred (see _decorate), never written by a runner.
RUNNING, DONE, ERROR, INTERRUPTED = "running", "done", "error", "interrupted"
# `cancelling` = a stop was requested and the work has not unwound yet;
# `cancelled` = it has. Both are written; `cancelled` is also inferred when a
# cancelling job's process disappears.
CANCELLING, CANCELLED = "cancelling", "cancelled"
IN_FLIGHT = (RUNNING, CANCELLING)      # statuses whose work may still be doing something
TERMINAL = (DONE, ERROR, INTERRUPTED, CANCELLED)

_LOCK = threading.Lock()

# Job ids cancelled in *this* process. The file is the cross-process signal, but
# an in-memory set means a worker never has to read the disk to notice its own
# job was stopped, and it works even if the registry file is unwritable.
_CANCEL_LOCAL: set[str] = set()


def registry_path(reports_root: str | Path | None = None) -> Path:
    return Path(reports_root or "reports") / REGISTRY_FILE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read(reports_root=None) -> list[dict]:
    try:
        data = json.loads(registry_path(reports_root).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write(records: list[dict], reports_root=None) -> None:
    path = registry_path(reports_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(records, indent=2), encoding="utf-8")
    os.replace(tmp, path)      # atomic: a concurrent reader never sees half a file


def _alive(pid) -> bool | None:
    """True/False if we can tell, None if the PID is unknown or not ours to check."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # exists, owned by someone else
    except Exception:
        return None


def register(
    job_type: str,
    *,
    job_id: str | None = None,
    pid: int | None = None,
    run_dir: str | Path | None = None,
    total: int | None = None,
    label: str = "",
    log_path: str | Path | None = None,
    params: dict | None = None,
    reports_root: str | Path | None = None,
) -> dict:
    """Record a job as running. ``pid`` defaults to this process."""
    record = {
        "id":         job_id or str(uuid.uuid4())[:8],
        "type":       job_type,
        "status":     RUNNING,
        "pid":        os.getpid() if pid is None else pid,
        "run_dir":    str(run_dir) if run_dir else None,
        "total":      total,
        "label":      label,
        "log_path":   str(log_path) if log_path else None,
        "params":     params or {},
        "started_at": _now(),
        "updated_at": _now(),
    }
    with _LOCK:
        records = [r for r in _read(reports_root) if r.get("id") != record["id"]]
        records.append(record)
        _write(_prune(records), reports_root)
    return record


def update(job_id: str, reports_root: str | Path | None = None, **fields) -> None:
    with _LOCK:
        records = _read(reports_root)
        for r in records:
            if r.get("id") == job_id:
                r.update(fields)
                r["updated_at"] = _now()
                break
        else:
            return
        _write(records, reports_root)


def finish(job_id: str, status: str = DONE, reports_root: str | Path | None = None, **fields) -> None:
    update(job_id, reports_root=reports_root, status=status, finished_at=_now(), **fields)


def request_cancel(job_id: str, reports_root: str | Path | None = None) -> dict | None:
    """Ask a job to stop. Returns the updated record, or None if unknown.

    This only *records intent* — it never kills anything. Stopping is cooperative
    because dashboard jobs run as threads inside the server process: their
    recorded PID is the server's own, so signalling it would take the dashboard
    down with the job. Workers call :func:`is_cancelled` between units of work.
    """
    _CANCEL_LOCAL.add(job_id)
    with _LOCK:
        records = _read(reports_root)
        for r in records:
            if r.get("id") == job_id:
                if r.get("status") in TERMINAL:
                    return r                      # already over; nothing to stop
                r["status"] = CANCELLING
                r["cancel_requested_at"] = _now()
                r["updated_at"] = _now()
                _write(records, reports_root)
                return r
    return None


def is_cancelled(job_id: str | None, reports_root: str | Path | None = None) -> bool:
    """Whether this job has been asked to stop. Cheap, and safe to call often.

    Checks this process first, then the file — so a job started by one process
    (a detached CLI resume) can be stopped from another (the dashboard).
    """
    if not job_id:
        return False
    if job_id in _CANCEL_LOCAL:
        return True
    for r in _read(reports_root):
        if r.get("id") == job_id:
            return r.get("status") in (CANCELLING, CANCELLED)
    return False


def _prune(records: list[dict]) -> list[dict]:
    """Drop finished records older than KEEP_FINISHED_DAYS; keep all running ones."""
    cutoff = datetime.now(timezone.utc).timestamp() - KEEP_FINISHED_DAYS * 86400
    out = []
    for r in records:
        if r.get("status") in IN_FLIGHT:
            out.append(r)
            continue
        try:
            ts = datetime.fromisoformat(r.get("finished_at") or r.get("updated_at") or "").timestamp()
        except Exception:
            ts = cutoff + 1        # unparseable → keep rather than silently drop
        if ts >= cutoff:
            out.append(r)
    return out


def _count_briefs(run_dir: str | Path) -> int | None:
    try:
        d = Path(run_dir)
        return sum(1 for t in d.iterdir() if t.is_dir() and (t / "earnings_brief.md").exists())
    except Exception:
        return None


def _decorate(record: dict) -> dict:
    """Add derived liveness + progress to a stored record."""
    out = dict(record)
    status = out.get("status")
    if status in IN_FLIGHT and _alive(out.get("pid")) is False:
        # A cancelling job whose process is gone got what was asked of it; a
        # running one that vanished did not — that distinction is what tells the
        # dashboard whether to offer a resume.
        out["status"] = CANCELLED if status == CANCELLING else INTERRUPTED

    done = _count_briefs(out["run_dir"]) if out.get("run_dir") else None
    total = out.get("total")
    if done is not None:
        pct = round(done / total * 100) if isinstance(total, int) and total > 0 else None
        out["progress"] = {"done": done, "total": total, "pct": pct}
    else:
        out["progress"] = None
    return out


def load(reports_root: str | Path | None = None) -> list[dict]:
    """All records, newest first, with derived status and progress."""
    return sorted((_decorate(r) for r in _read(reports_root)),
                  key=lambda r: r.get("started_at") or "", reverse=True)


def active(reports_root: str | Path | None = None) -> list[dict]:
    """Records whose work is genuinely still in flight (including stopping ones)."""
    return [r for r in load(reports_root) if r.get("status") in IN_FLIGHT]


def reap(reports_root: str | Path | None = None) -> int:
    """Persist inferred `interrupted`/`cancelled` states. Returns how many."""
    n = 0
    with _LOCK:
        records = _read(reports_root)
        for r in records:
            if r.get("status") in IN_FLIGHT and _alive(r.get("pid")) is False:
                r["status"] = CANCELLED if r.get("status") == CANCELLING else INTERRUPTED
                r["finished_at"] = _now()
                n += 1
        if n:
            _write(records, reports_root)
    return n
