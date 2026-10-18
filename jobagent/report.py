"""Tracking rows and exportable application reports (CSV, JSON, HTML)."""
from __future__ import annotations

import csv
import html
import io
import json
from typing import Any

from .db import Database, loads

COLUMNS = ["job_id", "title", "employer", "location", "remote_policy", "sources", "status", "meets_criteria",
           "disqualifiers", "relevance", "baseline_score", "final_score", "optimization_outcome", "resume_version",
           "missing_qualifications", "date_applied", "submission_verified", "reference", "application_url",
           "follow_up_date", "recruiter_response", "error", "retry_status", "blocker", "flags", "discovered_at"]


def rows(db: Database, status: str | None = None, search: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM jobs"
    params: list[Any] = []
    where = []
    if status:
        where.append("status=?")
        params.append(status)
    if search:
        where.append("(title LIKE ? OR employer LIKE ? OR location LIKE ? OR description LIKE ?)")
        params += [f"%{search}%"] * 4
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"
    out = []
    for j in db.q(sql, tuple(params)):
        screen = loads(j["screen_result"], {})
        base = db.latest_score(j["id"], "baseline")
        final = db.latest_score(j["id"], "final")
        app = db.application(j["id"])
        rv = db.resume_version(app["resume_version_id"]) if app and app["resume_version_id"] else None
        changes = loads(rv["changes"], {}) if rv else {}
        missing = loads(final["breakdown"], {}).get("missing_requirements", []) if final else []
        sources = sorted({r["source"] for r in db.q("SELECT source FROM job_sources WHERE job_id=?", (j["id"],))})
        out.append({
            "job_id": j["id"], "title": j["title"], "employer": j["employer"], "location": j["location"],
            "remote_policy": j["remote_policy"], "sources": ", ".join(sources), "status": j["status"],
            "meets_criteria": screen.get("passed") if screen else None,
            "disqualifiers": "; ".join(screen.get("disqualifiers", [])), "relevance": screen.get("relevance"),
            "baseline_score": base["total"] if base else None, "final_score": final["total"] if final else None,
            "optimization_outcome": changes.get("outcome") if isinstance(changes, dict) else None,
            "resume_version": rv["label"] if rv else None, "missing_qualifications": "; ".join(missing[:6]),
            "date_applied": app["submitted_at"] if app else None,
            "submission_verified": bool(app["verified"]) if app and app["submitted_at"] else None,
            "reference": app["reference"] if app else None, "application_url": j["application_url"],
            "follow_up_date": j["follow_up_date"], "recruiter_response": j["recruiter_response"],
            "error": app["last_error"] if app else None, "retry_status": app["retry_status"] if app else None,
            "blocker": app["blocker"] if app else None, "flags": ", ".join(loads(j["flags"], [])),
            "discovered_at": j["discovered_at"],
        })
    return out


def summary(db: Database) -> dict[str, Any]:
    total = db.one("SELECT COUNT(*) n FROM jobs")["n"]
    by_status = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
    meets = sum(1 for r in db.q("SELECT screen_result FROM jobs") if loads(r["screen_result"], {}).get("passed"))
    verified = db.one("SELECT COUNT(*) n FROM applications WHERE verified=1")["n"]
    return {"jobs_discovered": total, "meets_criteria": meets, "by_status": by_status, "verified_submissions": verified}


def to_csv(data: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in data:
        w.writerow(r)
    return buf.getvalue()


def to_json(data: list[dict], meta: dict) -> str:
    return json.dumps({"summary": meta, "applications": data}, indent=2, default=str)


def to_html(data: list[dict], meta: dict) -> str:
    head = "".join(f"<th>{html.escape(c)}</th>" for c in COLUMNS)
    body = "".join("<tr>" + "".join(f"<td>{html.escape('' if r.get(c) is None else str(r.get(c)))}</td>" for c in COLUMNS)
                   + "</tr>" for r in data)
    stats = " · ".join(f"{html.escape(k)}: {html.escape(str(v))}" for k, v in meta.items() if k != "by_status")
    status = ", ".join(f"{k} {v}" for k, v in sorted(meta.get("by_status", {}).items()))
    return ("<!doctype html><html><head><meta charset='utf-8'><title>Application report</title><style>"
            "body{font-family:Arial,sans-serif;font-size:12px;margin:16px}table{border-collapse:collapse}"
            "td,th{border:1px solid #ccc;padding:4px;vertical-align:top}th{background:#f2f2f2}</style></head><body>"
            f"<h1>Application report</h1><p>{stats}</p><p>{html.escape(status)}</p>"
            "<p>Match scores come from this tool's own rubric, not from any employer's applicant tracking system.</p>"
            f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></body></html>")


def export(db: Database, fmt: str) -> str:
    data, meta = rows(db), summary(db)
    if fmt == "csv":
        return to_csv(data)
    if fmt == "json":
        return to_json(data, meta)
    if fmt == "html":
        return to_html(data, meta)
    raise ValueError("format must be csv, json or html")
