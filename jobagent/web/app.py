"""Local tracking dashboard. Binds to 127.0.0.1 by default; set JOBAGENT_DASHBOARD_TOKEN to require a token."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from .. import pipeline, report
from ..config import Settings
from ..db import STATUSES, Database, loads

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
MANUAL_STATUSES = ["INTERVIEW", "OFFER", "REJECTED", "CLOSED"]


def create_app(settings: Settings, db: Database | None = None) -> FastAPI:
    db = db or Database(settings.db_path)
    token = os.environ.get("JOBAGENT_DASHBOARD_TOKEN")
    csrf = secrets.token_urlsafe(16)
    app = FastAPI(title="Job applications")

    @app.middleware("http")
    async def auth(request: Request, call_next):
        if token:
            supplied = request.cookies.get("jobagent_token") or request.query_params.get("token") or \
                request.headers.get("authorization", "").removeprefix("Bearer ")
            if not secrets.compare_digest(supplied or "", token):
                return PlainTextResponse("unauthorized", status_code=401)
            resp = await call_next(request)
            if request.query_params.get("token"):
                resp.set_cookie("jobagent_token", token, httponly=True, samesite="strict")
            return resp
        return await call_next(request)

    def check_csrf(value: str) -> None:
        if not secrets.compare_digest(value or "", csrf):
            raise HTTPException(403, "bad form token")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, status: str | None = None, q: str | None = None):
        data = report.rows(db, status or None, q or None)
        return TEMPLATES.TemplateResponse(request, "index.html", {
            "rows": data, "summary": report.summary(db), "statuses": STATUSES, "status": status or "", "q": q or "",
            "mode": settings.mode})

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(request: Request, job_id: int):
        job = db.job(job_id)
        if job is None:
            raise HTTPException(404)
        app_row = db.application(job_id)
        base, final = db.latest_score(job_id, "baseline"), db.latest_score(job_id, "final")
        rv = db.resume_version(app_row["resume_version_id"]) if app_row and app_row["resume_version_id"] else None
        versions = db.q("SELECT id, label, created_at, sha256 FROM resume_versions WHERE job_id=? ORDER BY id", (job_id,))
        audit = db.q("SELECT * FROM audit_log WHERE entity='job' AND entity_id=? ORDER BY id", (job_id,))
        sources = db.q("SELECT source, source_id, url, discovered_at FROM job_sources WHERE job_id=?", (job_id,))
        from ..documents import render_text
        from ..resume_doc import ResumeDoc
        return TEMPLATES.TemplateResponse(request, "job.html", {
            "job": job, "app": app_row, "screen": loads(job["screen_result"], {}),
            "baseline": loads(base["breakdown"], None) if base else None,
            "final": loads(final["breakdown"], None) if final else None,
            "base_total": base["total"] if base else None, "final_total": final["total"] if final else None,
            "changes": loads(rv["changes"], {}) if rv else {}, "files": loads(rv["files"], {}) if rv else {},
            "resume_text": render_text(ResumeDoc.from_dict(loads(rv["content"]))) if rv else "",
            "answers": loads(app_row["answers"], {}) if app_row else {}, "versions": versions, "audit": audit,
            "sources": sources, "verification": loads(app_row["verification"], {}) if app_row else {},
            "manual_statuses": MANUAL_STATUSES, "csrf": csrf, "flags": loads(job["flags"], [])})

    @app.post("/jobs/{job_id}/answer")
    def set_answer(job_id: int, question: str = Form(...), value: str = Form(...), csrf_token: str = Form("")):
        check_csrf(csrf_token)
        pipeline.set_answer(db, job_id, question, value, actor="candidate (dashboard)")
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/approve")
    def approve(job_id: int, csrf_token: str = Form("")):
        check_csrf(csrf_token)
        try:
            pipeline.approve(db, job_id, actor="candidate (dashboard)")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/tracking")
    def tracking(job_id: int, status: str = Form(""), follow_up_date: str = Form(""), recruiter_response: str = Form(""),
                 csrf_token: str = Form("")):
        check_csrf(csrf_token)
        fields = {}
        if follow_up_date:
            fields["follow_up_date"] = follow_up_date
        if recruiter_response:
            fields["recruiter_response"] = recruiter_response
        db.update_job(job_id, **fields)
        if fields:
            db.audit("candidate (dashboard)", "tracking_updated", "job", job_id, **fields)
        if status:
            if status not in MANUAL_STATUSES:
                raise HTTPException(400, "invalid status")
            db.set_status(job_id, status, "candidate (dashboard)", recruiter_response or "updated from dashboard")
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.get("/files/{job_id}/{kind}")
    def download(job_id: int, kind: str):
        app_row = db.application(job_id)
        rv = db.resume_version(app_row["resume_version_id"]) if app_row else None
        path = loads(rv["files"], {}).get(kind) if rv else None
        if not path or not Path(path).resolve().is_relative_to(settings.documents_dir.resolve()):
            raise HTTPException(404)
        return Response(Path(path).read_bytes(), headers={"Content-Disposition": f"attachment; filename={Path(path).name}"})

    @app.get("/export.{fmt}")
    def export(fmt: str):
        if fmt not in ("csv", "json", "html"):
            raise HTTPException(404)
        media = {"csv": "text/csv", "json": "application/json", "html": "text/html"}[fmt]
        return Response(report.export(db, fmt), media_type=media,
                        headers={"Content-Disposition": f"attachment; filename=applications.{fmt}"})

    @app.get("/audit", response_class=HTMLResponse)
    def audit(request: Request):
        rows = db.q("SELECT * FROM audit_log ORDER BY id DESC LIMIT 500")
        return TEMPLATES.TemplateResponse(request, "audit.html", {"rows": rows})

    return app
