"""SQLite persistence: schema, status machine and small repository helpers."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

STATUSES = (
    "DISCOVERED", "SCREENED", "SCORED", "OPTIMIZATION_REQUIRED", "OPTIMIZED",
    "READY_FOR_REVIEW", "APPROVED", "SUBMITTED", "SUBMISSION_UNVERIFIED", "FAILED",
    "REJECTED", "INTERVIEW", "OFFER", "CLOSED",
)

# Allowed forward transitions. CLOSED and REJECTED are reachable from anywhere.
_TRANSITIONS: dict[str, set[str]] = {
    "DISCOVERED": {"SCREENED"},
    "SCREENED": {"SCORED"},
    "SCORED": {"OPTIMIZATION_REQUIRED", "READY_FOR_REVIEW", "SCORED"},
    "OPTIMIZATION_REQUIRED": {"OPTIMIZED"},
    "OPTIMIZED": {"READY_FOR_REVIEW"},
    "READY_FOR_REVIEW": {"APPROVED", "SCORED", "READY_FOR_REVIEW"},
    "APPROVED": {"SUBMITTED", "SUBMISSION_UNVERIFIED", "FAILED", "READY_FOR_REVIEW"},
    "FAILED": {"APPROVED", "READY_FOR_REVIEW", "SUBMITTED", "SUBMISSION_UNVERIFIED", "FAILED"},
    "SUBMISSION_UNVERIFIED": {"SUBMITTED", "INTERVIEW", "OFFER"},
    "SUBMITTED": {"INTERVIEW", "OFFER"},
    "INTERVIEW": {"OFFER", "INTERVIEW"},
    "OFFER": set(),
    "REJECTED": set(),
    "CLOSED": set(),
}
ANYTIME = {"CLOSED", "REJECTED"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    blob BLOB NOT NULL,
    encrypted INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    dedup_key TEXT NOT NULL UNIQUE,
    canonical_url TEXT,
    title TEXT NOT NULL,
    employer TEXT NOT NULL,
    location TEXT,
    remote_policy TEXT,
    description TEXT,
    required_qualifications TEXT,
    preferred_qualifications TEXT,
    experience_requirement TEXT,
    salary_min REAL, salary_max REAL, salary_currency TEXT, salary_interval TEXT,
    employment_type TEXT,
    date_posted TEXT,
    closing_date TEXT,
    original_url TEXT,
    application_url TEXT,
    discovered_at TEXT NOT NULL,
    status TEXT NOT NULL,
    screen_result TEXT,
    flags TEXT,
    follow_up_date TEXT,
    recruiter_response TEXT,
    notes TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_canon ON jobs(canonical_url);
CREATE TABLE IF NOT EXISTS job_sources (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    url TEXT,
    discovered_at TEXT NOT NULL,
    raw TEXT,
    UNIQUE(source, source_id)
);
CREATE TABLE IF NOT EXISTS resume_versions (
    id INTEGER PRIMARY KEY,
    job_id INTEGER REFERENCES jobs(id),
    label TEXT NOT NULL,
    content TEXT NOT NULL,
    changes TEXT,
    files TEXT,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    resume_version_id INTEGER REFERENCES resume_versions(id),
    kind TEXT NOT NULL,
    total REAL NOT NULL,
    breakdown TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id),
    idempotency_key TEXT NOT NULL UNIQUE,
    resume_version_id INTEGER REFERENCES resume_versions(id),
    cover_letter TEXT,
    answers TEXT,
    unknowns TEXT,
    approved_at TEXT, approved_by TEXT,
    submitted_at TEXT,
    verified INTEGER NOT NULL DEFAULT 0,
    verification TEXT,
    reference TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    retry_status TEXT,
    next_retry_at TEXT,
    blocker TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consents (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    constraints TEXT NOT NULL,
    statement TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity TEXT,
    entity_id INTEGER,
    detail TEXT
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
"""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


class InvalidTransition(Exception):
    pass


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def q(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    # ---- audit -------------------------------------------------------
    def audit(self, actor: str, action: str, entity: str | None = None,
              entity_id: int | None = None, **detail: Any) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO audit_log(ts, actor, action, entity, entity_id, detail) VALUES (?,?,?,?,?,?)",
                (now(), actor, action, entity, entity_id, dumps(detail)),
            )

    # ---- jobs --------------------------------------------------------
    def job(self, job_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM jobs WHERE id=?", (job_id,))

    def set_status(self, job_id: int, new: str, actor: str = "system", reason: str = "", force: bool = False) -> None:
        if new not in STATUSES:
            raise ValueError(f"unknown status {new}")
        row = self.job(job_id)
        if row is None:
            raise KeyError(job_id)
        old = row["status"]
        if old == new and new not in _TRANSITIONS.get(old, set()):
            return
        if not force and new not in ANYTIME and new not in _TRANSITIONS.get(old, set()):
            raise InvalidTransition(f"job {job_id}: {old} -> {new} not allowed")
        with self.tx() as c:
            c.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (new, now(), job_id))
        self.audit(actor, "status_change", "job", job_id, old=old, new=new, reason=reason)

    def update_job(self, job_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = [dumps(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
        with self.tx() as c:
            c.execute(f"UPDATE jobs SET {cols}, updated_at=? WHERE id=?", (*vals, now(), job_id))

    # ---- resume versions --------------------------------------------
    def add_resume_version(self, job_id: int | None, label: str, content: dict, changes: list,
                           files: dict, sha256: str) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO resume_versions(job_id,label,content,changes,files,sha256,created_at) VALUES (?,?,?,?,?,?,?)",
                (job_id, label, dumps(content), dumps(changes), dumps(files), sha256, now()),
            )
            return int(cur.lastrowid)

    def resume_version(self, rv_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM resume_versions WHERE id=?", (rv_id,))

    def add_score(self, job_id: int, kind: str, total: float, breakdown: dict, rv_id: int | None) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO scores(job_id,resume_version_id,kind,total,breakdown,created_at) VALUES (?,?,?,?,?,?)",
                (job_id, rv_id, kind, total, dumps(breakdown), now()),
            )
            return int(cur.lastrowid)

    def latest_score(self, job_id: int, kind: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM scores WHERE job_id=? AND kind=? ORDER BY id DESC LIMIT 1", (job_id, kind))

    # ---- applications ------------------------------------------------
    def application(self, job_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM applications WHERE job_id=?", (job_id,))

    def upsert_application(self, job_id: int, idempotency_key: str, **fields: Any) -> int:
        existing = self.application(job_id)
        vals = {k: (dumps(v) if isinstance(v, (dict, list)) else v) for k, v in fields.items()}
        with self.tx() as c:
            if existing is None:
                cols = ["job_id", "idempotency_key", "created_at", "updated_at", *vals.keys()]
                ph = ",".join("?" for _ in cols)
                cur = c.execute(f"INSERT INTO applications({','.join(cols)}) VALUES ({ph})",
                                (job_id, idempotency_key, now(), now(), *vals.values()))
                return int(cur.lastrowid)
            if vals:
                sets = ", ".join(f"{k}=?" for k in vals)
                c.execute(f"UPDATE applications SET {sets}, updated_at=? WHERE job_id=?", (*vals.values(), now(), job_id))
            return int(existing["id"])

    def find_application_by_key(self, key: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM applications WHERE idempotency_key=?", (key,))

    # ---- consent -----------------------------------------------------
    def active_consent(self, kind: str = "auto_apply") -> sqlite3.Row | None:
        return self.one("SELECT * FROM consents WHERE kind=? AND revoked_at IS NULL ORDER BY id DESC LIMIT 1", (kind,))
