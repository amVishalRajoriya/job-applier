"""Background runs started from the dashboard: one at a time, with a log the page polls.

Each run uses its own database connection and its own browser, so the dashboard stays responsive.
The same pipeline functions as the CLI do the work; nothing here decides what may be submitted.
"""
from __future__ import annotations

import itertools
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .. import pipeline
from .. import profile as P
from ..apply import engine
from ..config import Settings
from ..db import Database


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


@dataclass
class Task:
    id: int
    kind: str
    label: str
    status: str = "queued"  # queued | running | done | failed
    log: list[str] = field(default_factory=list)
    result: Any = None
    job_id: int | None = None
    opts: dict = field(default_factory=dict)
    started_at: str | None = None
    finished_at: str | None = None

    def say(self, line: str) -> None:
        self.log.append(f"{_ts()}  {line}")

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "label": self.label, "status": self.status, "log": self.log,
                "result": self.result, "job_id": self.job_id, "started_at": self.started_at,
                "finished_at": self.finished_at}


class Runner:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.tasks: dict[int, Task] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def busy(self) -> Task | None:
        return next((t for t in self.tasks.values() if t.status in ("queued", "running")), None)

    def recent(self, n: int = 8) -> list[Task]:
        return sorted(self.tasks.values(), key=lambda t: -t.id)[:n]

    def start(self, kind: str, job_id: int | None = None, wait: bool = False, **opts) -> Task:
        fn, label = ACTIONS[kind]
        with self._lock:
            running = self.busy()
            if running:
                raise RuntimeError(f"'{running.label}' is still running; wait for it to finish")
            t = Task(next(self._ids), kind, label + (f" (job {job_id})" if job_id else ""), job_id=job_id,
                     opts=opts)
            self.tasks[t.id] = t
        th = threading.Thread(target=self._run, args=(t, fn), daemon=True)
        th.start()
        if wait:
            th.join()
        return t

    def _run(self, t: Task, fn: Callable) -> None:
        t.status, t.started_at = "running", _ts()
        db = Database(self.settings.db_path)
        try:
            t.result = fn(t, db, self.settings)
            t.status = "done"
        except Exception as e:
            t.say(f"stopped with an error: {type(e).__name__}: {e}")
            t.result = {"error": str(e), "trace": traceback.format_exc()[-2000:]}
            t.status = "failed"
        finally:
            t.finished_at = _ts()
            db.conn.close()


# ---- actions ----------------------------------------------------------------------------------

def _check_sources(t: Task, db: Database, s: Settings):
    if not s.sources:
        t.say("no job boards configured yet: add them on the Setup page")
        return []
    t.say(f"fetching {len(s.sources)} job boards once; nothing is stored")
    results = pipeline.check_sources(s)
    for r in results:
        t.say(f"OK   {r['source']}: {r['jobs']} jobs" if r["ok"] else f"FAIL {r['source']}: {r['error']}")
        for n in r.get("notes", []):
            t.say(f"     note: {n[:300]}")
    t.say(f"{sum(r['ok'] for r in results)} of {len(results)} boards reachable")
    return results


def _discover(t: Task, db: Database, s: Settings):
    t.say(f"discovering jobs from {len(s.sources)} job board(s)")
    out = pipeline.run_discovery(db, s)
    t.say(f"fetched {out['fetched']}, new {out['new_jobs']}, duplicates merged {out['merged_duplicates']}")
    for e in out["errors"]:
        t.say(f"FAIL {e['source']}: {e['error'][:300]}")
    for n in out.get("notes", []):
        t.say(f"note {n['source']}: {n['note'][:300]}")
    return out


def _process(t: Task, db: Database, s: Settings):
    t.say("screening, scoring and preparing documents for new jobs (this opens each application form "
          "read-only to collect its questions)")
    out = pipeline.screen_and_score(db, s, P.load(db))
    t.say(f"screened {out['screened']}, met your criteria {out['passed']}, ready for your review {out['ready']}, "
          f"closed {out['closed']}")
    for e in out.get("errors", []):
        t.say(f"problem: {e}")
    return out


def _find(t: Task, db: Database, s: Settings):
    a = _discover(t, db, s)
    b = _process(t, db, s)
    t.say("done: open the Jobs page to review what was prepared")
    return {"discovery": a, "processing": b}


def _apply(t: Task, db: Database, s: Settings):
    if s.mode != "review":
        t.say(f"mode is '{s.mode}': switch to review mode to submit applications you approved")
        return []
    ids = [t.job_id] if t.job_id else None
    results = pipeline.apply_pending(db, s, P.load(db), ids)
    if not results:
        t.say("no approved applications waiting")
    for r in results:
        line = f"job {r.get('job_id')}: {r['outcome']}"
        if r.get("reference"):
            line += f", reference {r['reference']}"
        if r.get("detail"):
            line += f" ({r['detail']})"
        t.say(line)
    return results


def _dry_run(t: Task, db: Database, s: Settings):
    t.say(f"opening the real application page for job {t.job_id} read-only; nothing will be submitted")
    show = bool(t.opts.get("show"))
    if show:
        t.say("the browser window stays open for 60 seconds after filling so you can look at it")
    rep = engine.dry_run(db, s, P.load(db), t.job_id, actor="candidate (dashboard)", headful=True if show else None,
                         hold=(lambda: time.sleep(60)) if show else None)
    t.say(f"{rep['outcome']}: {rep['detail']}")
    for f in rep["fields"]:
        t.say(f"  {f['action']}: {f['field']} = {str(f['value'])[:80]}")
    for q in rep["open_questions"]:
        t.say(f"  open question: {q['question']}")
    t.say(f"requests blocked because they could send data: {len(rep['blocked_requests'])}. Nothing was submitted.")
    return rep


ACTIONS: dict[str, tuple[Callable, str]] = {
    "check_sources": (_check_sources, "Check job boards"),
    "discover": (_discover, "Discover jobs"),
    "process": (_process, "Score and prepare"),
    "find": (_find, "Find and prepare jobs"),
    "apply": (_apply, "Submit approved applications"),
    "dry_run": (_dry_run, "Dry run"),
}
