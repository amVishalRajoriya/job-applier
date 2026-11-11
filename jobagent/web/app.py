"""Local tracking dashboard. Binds to 127.0.0.1 by default; set JOBAGENT_DASHBOARD_TOKEN to require a token."""
from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from .. import onboarding, pipeline, report
from .. import profile as P
from ..config import Settings
from ..db import STATUSES, Database, InvalidTransition, loads
from .tasks import ACTIONS, Runner

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
MANUAL_STATUSES = ["INTERVIEW", "OFFER", "REJECTED", "CLOSED"]


def create_app(settings: Settings, db: Database | None = None) -> FastAPI:
    db = db or Database(settings.db_path)
    token = os.environ.get("JOBAGENT_DASHBOARD_TOKEN")
    csrf = secrets.token_urlsafe(16)
    app = FastAPI(title="Job applications")
    runner = Runner(settings)
    app.state.runner = runner
    config_path = settings.home / "config.yaml"

    hosted = os.environ.get("JOBAGENT_HOSTED") == "1"
    TEMPLATES.env.globals["hosted"] = hosted
    TEMPLATES.env.globals["locked"] = bool(token)

    def set_login_cookie(resp: Response, request: Request) -> None:
        resp.set_cookie("jobagent_token", token, httponly=True, samesite="lax", max_age=30 * 86400,
                        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")

    @app.middleware("http")
    async def auth(request: Request, call_next):
        if token and request.url.path not in ("/login", "/healthz"):
            supplied = request.cookies.get("jobagent_token") or request.query_params.get("token") or \
                request.headers.get("authorization", "").removeprefix("Bearer ")
            if not secrets.compare_digest(supplied or "", token):
                if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
                    return RedirectResponse("/login?next=" + quote(request.url.path), status_code=303)
                return PlainTextResponse("unauthorized", status_code=401)
            resp = await call_next(request)
            if request.query_params.get("token"):
                set_login_cookie(resp, request)
            return resp
        return await call_next(request)

    def safe_next(nxt: str) -> str:
        return nxt if nxt.startswith("/") and not nxt.startswith("//") else "/"

    @app.get("/healthz")
    def healthz():
        return PlainTextResponse("ok")

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/", err: str = ""):
        return TEMPLATES.TemplateResponse(request, "login.html", {"next": safe_next(next), "err": err})

    @app.post("/login")
    def login(request: Request, password: str = Form(""), next: str = Form("/")):
        if not token or secrets.compare_digest(password, token):
            resp = RedirectResponse(safe_next(next), status_code=303)
            if token:
                set_login_cookie(resp, request)
            return resp
        return RedirectResponse("/login?err=1&next=" + quote(safe_next(next)), status_code=303)

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie("jobagent_token")
        return resp

    def check_csrf(value: str) -> None:
        if not secrets.compare_digest(value or "", csrf):
            raise HTTPException(403, "bad form token")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, status: str | None = None, q: str | None = None):
        data = report.rows(db, status or None, q or None)
        return TEMPLATES.TemplateResponse(request, "index.html", {
            "rows": data, "summary": report.summary(db), "statuses": STATUSES, "status": status or "", "q": q or "",
            "mode": settings.mode, "has_profile": db.one("SELECT 1 FROM profile WHERE id=1") is not None})

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(request: Request, job_id: int, task: int | None = None, err: str = ""):
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
        from ..apply import assisted
        kit = None
        if assisted.is_assisted(job):
            try:
                kit = assisted.kit(db, P.load(db), job_id)
            except LookupError:
                kit = None
        return TEMPLATES.TemplateResponse(request, "job.html", {
            "kit": kit,
            "job": job, "app": app_row, "screen": loads(job["screen_result"], {}),
            "baseline": loads(base["breakdown"], None) if base else None,
            "final": loads(final["breakdown"], None) if final else None,
            "base_total": base["total"] if base else None, "final_total": final["total"] if final else None,
            "changes": loads(rv["changes"], {}) if rv else {}, "files": loads(rv["files"], {}) if rv else {},
            "resume_text": render_text(ResumeDoc.from_dict(loads(rv["content"]))) if rv else "",
            "answers": loads(app_row["answers"], {}) if app_row else {}, "versions": versions, "audit": audit,
            "sources": sources, "verification": loads(app_row["verification"], {}) if app_row else {},
            "manual_statuses": MANUAL_STATUSES, "csrf": csrf, "flags": loads(job["flags"], []),
            "dry": latest_dry_run(job_id), "mode": settings.mode, "err": err,
            "task": runner.tasks.get(task) if task else None,
            "can_dry_run": bool(app_row and app_row["resume_version_id"])})

    def latest_dry_run(job_id: int) -> dict | None:
        row = db.one("SELECT detail FROM audit_log WHERE action='dry_run' AND entity='job' AND entity_id=? "
                     "ORDER BY id DESC LIMIT 1", (job_id,))
        if not row:
            return None
        path = Path(loads(row["detail"], {}).get("report", "")).with_name("report.json")
        if not path.exists():
            return None
        import json
        return json.loads(path.read_text())

    @app.get("/jobs/{job_id}/dry-run.png")
    def dry_run_screenshot(job_id: int):
        rep = latest_dry_run(job_id)
        shot = Path((rep or {}).get("evidence", {}).get("screenshot") or "")
        if not shot.is_file() or not shot.resolve().is_relative_to(settings.documents_dir.resolve()):
            raise HTTPException(404)
        return Response(shot.read_bytes(), media_type="image/png")

    def start(kind: str, job_id: int | None = None, **opts):
        try:
            return runner.start(kind, job_id, **opts), None
        except RuntimeError as e:
            return None, str(e)

    @app.post("/jobs/{job_id}/dry-run")
    def job_dry_run(job_id: int, csrf_token: str = Form(""), show: str = Form("")):
        check_csrf(csrf_token)
        t, err = start("dry_run", job_id, show=bool(show))
        return RedirectResponse(f"/jobs/{job_id}?" + (f"task={t.id}" if t else f"err={quote(err)}"), status_code=303)

    @app.post("/jobs/{job_id}/submit")
    def job_submit(job_id: int, csrf_token: str = Form("")):
        check_csrf(csrf_token)
        if settings.mode != "review":
            raise HTTPException(400, "switch to review mode on the Run page to submit applications you approved")
        if db.job(job_id) is None:
            raise HTTPException(404, "job not found")
        if db.job(job_id)["status"] != "APPROVED":
            raise HTTPException(400, "approve this application first")
        t, err = start("apply", job_id)
        return RedirectResponse(f"/jobs/{job_id}?" + (f"task={t.id}" if t else f"err={quote(err)}"), status_code=303)

    # ---- running the agent -------------------------------------------------------------------
    @app.get("/run", response_class=HTMLResponse)
    def run_page(request: Request, task: int | None = None, err: str = "", msg: str = ""):
        try:
            prof = P.load(db)
        except LookupError:
            prof = None
        enc = db.one("SELECT encrypted FROM profile WHERE id=1")
        counts = report.summary(db)
        return TEMPLATES.TemplateResponse(request, "run.html", {
            "csrf": csrf, "mode": settings.mode, "err": err, "msg": msg, "profile": prof,
            "encrypted": bool(enc and enc["encrypted"]), "todo": onboarding.unanswered(prof) if prof else [],
            "sources": settings.sources, "summary": counts,
            "approved": db.one("SELECT COUNT(*) n FROM jobs WHERE status='APPROVED'")["n"],
            "task": runner.tasks.get(task) if task else runner.busy(), "recent": runner.recent(),
            "llm": settings.llm_enabled})

    @app.post("/run/{kind}")
    def run_action(kind: str, csrf_token: str = Form("")):
        check_csrf(csrf_token)
        if kind not in ACTIONS or kind == "dry_run":
            raise HTTPException(404)
        t, err = start(kind)
        return RedirectResponse("/run?" + (f"task={t.id}" if t else f"err={quote(err)}"), status_code=303)

    @app.get("/tasks/{task_id}")
    def task_status(task_id: int):
        t = runner.tasks.get(task_id)
        if not t:
            raise HTTPException(404)
        d = t.to_dict()
        d.pop("result", None)
        return d

    @app.post("/settings/mode")
    def set_mode(mode: str = Form(...), csrf_token: str = Form("")):
        check_csrf(csrf_token)
        if mode not in ("discovery", "review"):
            raise HTTPException(400, "auto-apply is turned on only with `jobagent consent grant` and config.yaml")
        onboarding.update_config(config_path, mode=mode)
        settings.mode = mode
        db.audit("candidate (dashboard)", "mode_changed", None, None, mode=mode)
        return RedirectResponse("/run?msg=" + quote(f"mode set to {mode}"), status_code=303)

    @app.post("/jobs/{job_id}/manual-submitted")
    def manual_submitted(job_id: int, reference: str = Form(""), csrf_token: str = Form("")):
        """The candidate submitted a LinkedIn / Indeed / Naukri application themselves."""
        check_csrf(csrf_token)
        from ..apply import assisted
        if db.job(job_id) is None:
            raise HTTPException(404)
        assisted.record_manual_submission(db, job_id, reference.strip() or None, actor="candidate (dashboard)")
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

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
        if db.job(job_id) is None:
            raise HTTPException(404, "job not found")
        if status and status not in MANUAL_STATUSES:
            raise HTTPException(400, "invalid status")
        fields = {}
        if follow_up_date:
            fields["follow_up_date"] = follow_up_date
        if recruiter_response:
            fields["recruiter_response"] = recruiter_response
        db.update_job(job_id, **fields)
        if fields:
            db.audit("candidate (dashboard)", "tracking_updated", "job", job_id, **fields)
        if status:
            try:
                db.set_status(job_id, status, "candidate (dashboard)", recruiter_response or "updated from dashboard")
            except InvalidTransition:
                cur = db.job(job_id)["status"]
                hint = (" Mark the application as submitted first." if status in ("INTERVIEW", "OFFER") else "")
                return RedirectResponse(f"/jobs/{job_id}?err=" + quote(f"Cannot move this job from {cur} to {status}.{hint}"),
                                        status_code=303)
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

    # ---- first-run setup ------------------------------------------------------------------------

    def current_sources() -> list[dict]:
        import yaml
        return (yaml.safe_load(config_path.read_text()) or {}).get("sources") or [] if config_path.exists() else []

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request, msg: str = "", err: str = ""):
        try:
            prof = P.load(db)
        except LookupError:
            prof = None
        srcs = current_sources()
        from ..sources.jobsites import SavedPagesSource, queued_urls
        js = onboarding.jobsite_sources(srcs)
        pages, alerts = js.get("saved_pages") or {}, js.get("job_alerts") or {}
        queued = queued_urls(pages.get("urls_file")) if pages else []
        pending = []
        if pages and Path(pages["folder"]).expanduser().is_dir():
            src = SavedPagesSource(None, **{k: v for k, v in pages.items() if k != "type"})
            try:
                list(src.fetch())
                pending = src.pending
            except Exception:
                pending = queued
        return TEMPLATES.TemplateResponse(request, "setup.html", {
            "jobsites": {"pages_folder": pages.get("folder") or str(settings.saved_pages_dir),
                         "alerts_folder": alerts.get("folder") or str(settings.alerts_dir),
                         "imap": alerts.get("imap") or {}, "pages_on": bool(pages), "alerts_on": bool(alerts),
                         "queued": queued, "pending": pending},
            "profile": prof, "rows": onboarding.form_rows(prof) if prof else [], "csrf": csrf, "msg": msg, "err": err,
            "todo": onboarding.unanswered(prof) if prof else [],
            "profile_yaml": P.to_yaml(prof) if prof else "",
            "boards": "\n".join(line for c in srcs for line in onboarding.source_to_entry(c)),
            "kept_sources": [c for c in srcs if not onboarding.source_to_entry(c)],
            "levels": ["unknown", "exposure", "beginner", "intermediate", "advanced", "expert"]})

    def back(msg: str = "", err: str = "") -> RedirectResponse:
        from urllib.parse import urlencode
        return RedirectResponse("/setup?" + urlencode({"msg": msg, "err": err}), status_code=303)

    @app.post("/setup/resume")
    async def setup_resume(file: UploadFile = File(...), csrf_token: str = Form("")):
        check_csrf(csrf_token)
        from ..resume_parser import parse_resume
        ext = Path(file.filename or "").suffix.lower()
        if ext not in (".pdf", ".docx", ".txt", ".md"):
            return back(err="upload a PDF (with a text layer), DOCX, TXT or MD file")
        data = await file.read()
        if len(data) > 10 * 1024 * 1024:
            return back(err="file is larger than 10 MB")
        up = settings.home / "uploads"
        up.mkdir(mode=0o700, exist_ok=True)
        path = up / f"master_resume{ext}"
        path.write_bytes(data)
        try:
            parsed, _ = parse_resume(str(path))
        except Exception as e:
            return back(err=f"could not read the resume: {e}")
        try:
            parsed = onboarding.merge_resume(P.load(db), parsed)
        except LookupError:
            pass
        P.save(db, parsed, actor="candidate (dashboard setup)")
        custom = onboarding.unknown_skills(parsed)
        if custom:
            onboarding.update_config(config_path, custom_skills=custom)
        return back(msg=f"resume read: {len(parsed.experience)} roles, {sum(len(e.bullets) for e in parsed.experience)} "
                        "bullets. Check the parsed profile below.")

    @app.post("/setup/details")
    async def setup_details(request: Request):
        form = await request.form()
        check_csrf(str(form.get("csrf_token", "")))
        try:
            prof = P.load(db)
        except LookupError:
            return back(err="upload your resume first")
        details: dict = {}
        for q in onboarding.QUESTIONS:
            v = str(form.get(q["key"], "")).strip()
            if v:
                onboarding._set(details, q["key"], v)
        details["skills"] = {k[6:]: str(v) for k, v in form.items() if k.startswith("skill:") and v and v != "unknown"}
        try:
            prof = onboarding.apply_details(prof, details)
        except ValueError as e:
            return back(err=str(e))
        P.save(db, prof, actor="candidate (dashboard setup)")
        boards = [x for x in str(form.get("boards", "")).splitlines() if x.strip()]
        sources, problems = onboarding.boards_from_entries(boards)
        kept = [c for c in current_sources() if not onboarding.source_to_entry(c)]
        mode = str(form.get("mode", "")) or None
        try:
            onboarding.update_config(config_path, sources=kept + sources, mode=mode)
        except ValueError as e:
            return back(err=str(e))
        settings.sources = kept + sources
        if mode:
            settings.mode = mode
        return back(msg="saved" + (f"; skipped: {'; '.join(problems)}" if problems else ""))

    @app.post("/setup/jobsites")
    def setup_jobsites(pages_folder: str = Form(""), alerts_folder: str = Form(""), imap_host: str = Form(""),
                       imap_user: str = Form(""), imap_password_env: str = Form(""), urls: str = Form(""),
                       csrf_token: str = Form("")):
        """LinkedIn / Indeed / Naukri inputs: saved-pages folder, alert emails, pasted job URLs. Nothing is fetched."""
        check_csrf(csrf_token)
        from ..sources.jobsites import add_urls
        imap = None
        if imap_host.strip() and imap_user.strip():
            env = imap_password_env.strip() or "JOBAGENT_IMAP_PASSWORD"
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", env):
                return back(err="the password setting takes the NAME of an environment variable, e.g. "
                                "JOBAGENT_IMAP_PASSWORD, never the password itself")
            imap = {"host": imap_host.strip(), "user": imap_user.strip(), "password_env": env,
                    "mailbox": "INBOX", "since_days": 14}
        pages = pages_folder.strip() or (str(settings.saved_pages_dir) if urls.strip() else "")
        try:
            new = onboarding.set_jobsite_sources(config_path, pages_folder=pages or None,
                                                 urls_file=str(settings.urls_file) if pages else None,
                                                 alerts_folder=alerts_folder.strip() or None, imap=imap)
        except (ValueError, OSError) as e:
            return back(err=str(e))
        settings.sources = new
        added, rejected = add_urls(settings.urls_file, urls.splitlines()) if urls.strip() else ([], [])
        db.audit("candidate (dashboard)", "jobsite_inputs_saved", None, None, pages=bool(pages),
                 alerts=bool(alerts_folder.strip() or imap), queued=len(added))
        msg = "saved" + (f"; queued {len(added)} job URL(s)" if added else "")
        return back(msg=msg, err=("not a LinkedIn/Indeed/Naukri job URL: " + "; ".join(rejected)) if rejected else "")

    @app.post("/setup/profile")
    def setup_profile(profile_yaml: str = Form(...), csrf_token: str = Form("")):
        check_csrf(csrf_token)
        import yaml
        try:
            prof = P.Profile.model_validate(yaml.safe_load(profile_yaml))
        except Exception as e:
            return back(err=f"profile not saved: {str(e)[:500]}")
        P.save(db, prof, actor="candidate (dashboard setup)")
        return back(msg="profile saved")

    @app.get("/audit", response_class=HTMLResponse)
    def audit(request: Request):
        rows = db.q("SELECT * FROM audit_log ORDER BY id DESC LIMIT 500")
        return TEMPLATES.TemplateResponse(request, "audit.html", {"rows": rows})

    return app
