"""Application workflow: authorization, idempotency, form filling, submission and verification.

An application is recorded as SUBMITTED only when the confirmation page shows
a success message and a reference number. Anything less is
SUBMISSION_UNVERIFIED. A crash between clicking submit and recording the
result is detected on the next run and never resubmitted blindly.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dataclasses import replace
from typing import Callable, Iterator

from ..answers import answer as answer_question
from ..browser import launch_chromium
from ..config import Settings
from ..db import Database, dumps, loads, now
from ..profile import Profile
from playwright.sync_api import Error as PWError

from ..security import host_of
from .adapters import ADAPTERS, FatalError, FormField, GenericFormAdapter, NeedsHuman, TransientError
from ..sources.base import CAPABILITIES
from .policy import allowed_domains_for, authorize, capability_for

DONE = ("SUBMITTED", "SUBMISSION_UNVERIFIED", "INTERVIEW", "OFFER")
ASSISTED_NOTE = ("{site} applications are never automated: open the application kit (`jobagent assist JOB_ID` or "
                 "the job page in the dashboard), submit on the site yourself, then mark it as submitted")


@contextmanager
def isolated_page(settings: Settings) -> Iterator:
    """Fresh browser context per application: no shared cookies, storage or downloads."""
    with launch_chromium(headless=settings.browser_headless) as (_, browser):
        ctx = browser.new_context(accept_downloads=False, java_script_enabled=True,
                                  user_agent=settings.user_agent + " (candidate-authorized application)")
        try:
            yield ctx.new_page()
        finally:
            ctx.close()


def build_answers(fields: list[FormField], profile: Profile, job: dict, existing: dict | None = None) -> dict:
    existing = existing or {}
    out = {}
    for f in fields:
        prev = existing.get(f.label)
        if prev and prev.get("source") == "candidate":
            out[f.label] = {**prev, "required": f.required, "type": f.type, "options": f.options}
            continue
        if f.type == "file":
            kind = "cover_letter" if "cover" in f.label.lower() else "resume" if any(
                k in f.label.lower() for k in ("resume", "cv")) else "unknown_file"
            out[f.label] = {"kind": kind, "value": None, "status": "file" if kind != "unknown_file" else "needs_input",
                            "source": "documents", "required": f.required, "type": f.type, "options": []}
            continue
        a = answer_question(f.label, profile, job, f.options or None)
        status = a.status
        if f.options and a.value is not None and a.value not in f.options and f.type in ("select", "radio"):
            status, note = "needs_input", f"answer '{a.value}' is not one of the options {f.options}"
        else:
            note = a.note
        out[f.label] = {"kind": a.kind, "value": a.value, "status": status, "source": a.source, "note": note,
                        "required": f.required, "type": f.type, "options": f.options}
    return out


def _open_and_check(a: GenericFormAdapter, job) -> None:
    """Open the application page and stop on anything that is not a fillable, single-page form for this job."""
    a.open(job["application_url"])
    a.check_open()
    a.verify_identity(job["employer"], job["title"])
    blockers = a.detect_blockers()
    if blockers:
        raise NeedsHuman("; ".join(blockers))
    a.check_single_page()


def _open_questions(current: dict, answers: dict) -> tuple[list[str], list[str]]:
    """Required questions not seen when the application was prepared, and required questions still unanswered."""
    new_q = [lbl for lbl, v in current.items() if lbl not in answers and v["required"]]
    unresolved = [lbl for lbl, v in current.items() if v["required"] and v["status"] not in ("answered", "file")]
    return new_q, unresolved


def _fill_form(a: GenericFormAdapter, fields: list[FormField], current: dict, files: dict,
               strict: bool = True, resume_format: str = "docx") -> list[dict]:
    """Fill answered fields and upload documents. Returns what was done, field by field."""
    log = []
    for f in fields:
        v = current[f.label]
        if v["status"] == "file":
            path = (files.get(resume_format) or files.get("docx")) if v["kind"] == "resume" \
                else files.get("cover_letter_docx")
            if path and Path(path).exists():
                a.upload(f, path)
                log.append({"field": f.label, "action": "uploaded", "value": Path(path).name})
            elif f.required and strict:
                raise NeedsHuman(f"no document available for required upload '{f.label}'")
            else:
                log.append({"field": f.label, "action": "left empty", "value": "no document available"})
        elif v["status"] == "answered" and v["value"] not in (None, ""):
            a.fill(f, str(v["value"]))
            log.append({"field": f.label, "action": "filled", "value": str(v["value"]), "source": v.get("source")})
        else:
            log.append({"field": f.label, "action": "left empty", "value": v.get("note") or v["status"],
                        "required": f.required})
    return log


def inspect_form(settings: Settings, job) -> list[FormField]:
    """Open the form without filling or submitting, to collect its questions for review."""
    url = job["application_url"]
    key, apply_status, _ = capability_for(url)
    if apply_status == "assisted":
        raise NeedsHuman(ASSISTED_NOTE.format(site=CAPABILITIES[key].name))
    if apply_status not in ("tested_local", "untested_live"):
        raise NeedsHuman("no automated adapter for this site: apply manually")
    for attempt in range(1, settings.max_apply_attempts + 1):
        try:
            with isolated_page(settings) as page:
                a = GenericFormAdapter(page, allowed_domains_for(url, settings.extra_allowed_domains),
                                       settings.browser_timeout_ms, read_only=True)
                _open_and_check(a, job)
                return a.fields()
        except TransientError:
            if attempt == settings.max_apply_attempts:
                raise
            time.sleep(min(2 ** attempt * 0.25, 5))
    return []


def _evidence(page, out_dir: Path, tag: str) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    shot, html = out_dir / f"{tag}_{ts}.png", out_dir / f"{tag}_{ts}.html"
    try:
        page.screenshot(path=str(shot), full_page=True)
        html.write_text(page.content())
    except Exception:
        return {}
    return {"screenshot": str(shot), "html": str(html), "url": page.url}


def submit(db: Database, settings: Settings, profile: Profile, job_id: int, actor: str = "agent") -> dict:
    job = db.job(job_id)
    app = db.application(job_id)

    # ---- idempotency --------------------------------------------------------------------------
    if job["status"] in DONE or (app and app["submitted_at"]):
        db.audit(actor, "duplicate_submission_blocked", "job", job_id, status=job["status"])
        return {"outcome": "duplicate_blocked", "status": job["status"],
                "detail": "an application for this job was already submitted"}
    if app and app["retry_status"] == "in_progress":
        db.upsert_application(job_id, app["idempotency_key"], retry_status="needs_manual_check",
                              last_error="previous run stopped after clicking submit; outcome unknown")
        db.set_status(job_id, "SUBMISSION_UNVERIFIED", actor, "interrupted during submission", force=True)
        return {"outcome": "unverified", "status": "SUBMISSION_UNVERIFIED",
                "detail": "a previous attempt may have submitted; check the employer portal or email"}
    same_url = db.one("SELECT j.id FROM jobs j JOIN applications a ON a.job_id=j.id WHERE j.application_url=? "
                      "AND j.id<>? AND a.submitted_at IS NOT NULL", (job["application_url"], job_id))
    if same_url:
        db.audit(actor, "duplicate_submission_blocked", "job", job_id, other_job=same_url["id"])
        return {"outcome": "duplicate_blocked", "status": job["status"],
                "detail": f"already applied through the same application URL (job {same_url['id']})"}

    # ---- authorization ------------------------------------------------------------------------------
    decision = authorize(db, settings.mode, job_id)
    if not decision.allowed:
        db.audit(actor, "submission_not_authorized", "job", job_id, reason=decision.reason, mode=settings.mode)
        if decision.review_needed and app is not None:
            db.upsert_application(job_id, app["idempotency_key"], blocker=decision.reason)
            if job["status"] not in ("READY_FOR_REVIEW",):
                db.set_status(job_id, "READY_FOR_REVIEW", actor, decision.reason, force=job["status"] == "FAILED")
        return {"outcome": "not_authorized", "status": db.job(job_id)["status"], "detail": decision.reason}
    if job["status"] != "APPROVED":
        if job["status"] == "FAILED":
            db.set_status(job_id, "APPROVED", actor, "retry")
        else:
            db.set_status(job_id, "APPROVED", "auto-apply policy", decision.reason, force=True)
            db.upsert_application(job_id, app["idempotency_key"], approved_at=now(), approved_by="auto-apply policy")
    db.audit(actor, "submission_authorized", "job", job_id, reason=decision.reason, mode=settings.mode)

    url = job["application_url"]
    key, apply_status, _ = capability_for(url)
    if apply_status == "assisted":
        return _pause(db, job_id, app, actor, ASSISTED_NOTE.format(site=CAPABILITIES[key].name))
    if apply_status not in ("tested_local", "untested_live") or "generic_form" not in ADAPTERS:
        return _pause(db, job_id, app, actor, f"{key}: no automated adapter ({apply_status}); apply manually using "
                      "the prepared documents")
    rv = db.resume_version(app["resume_version_id"])
    files = loads(rv["files"], {})
    answers = loads(app["answers"], {})
    out_dir = settings.documents_dir / f"job_{job_id}"
    allowed = allowed_domains_for(url, settings.extra_allowed_domains)

    last_error = None
    for attempt in range(1, settings.max_apply_attempts + 1):
        db.upsert_application(job_id, app["idempotency_key"], attempts=(db.application(job_id)["attempts"] or 0) + 1)
        try:
            with isolated_page(settings) as page:
                a = GenericFormAdapter(page, allowed, settings.browser_timeout_ms)
                try:
                    _open_and_check(a, job)
                    fields = a.fields()
                    current = build_answers(fields, profile, dict(job), answers)
                    new_q, unresolved = _open_questions(current, answers)
                    if new_q or unresolved:
                        merged = {**current, **{k: v for k, v in answers.items() if k in current and v.get("source") == "candidate"}}
                        db.upsert_application(job_id, app["idempotency_key"], answers=merged)
                        raise NeedsHuman("questions need the candidate: " + "; ".join(sorted(set(new_q + unresolved))),
                                         [{"question": q, **current[q]} for q in sorted(set(new_q + unresolved))])
                    _fill_form(a, fields, current, files, resume_format=settings.upload_format)
                    invalid = a.invalid_required()
                    if invalid:
                        raise NeedsHuman(f"form still has invalid or empty fields: {invalid}")
                    db.upsert_application(job_id, app["idempotency_key"], retry_status="in_progress")
                    db.audit(actor, "submit_clicked", "job", job_id, attempt=attempt)
                    try:
                        a.submit()
                    except TimeoutError:
                        ev = _evidence(page, out_dir, "after_submit_timeout")
                        return _record(db, job_id, app, actor, "SUBMISSION_UNVERIFIED", None, ev,
                                       "no response after submit; outcome unknown")
                    except PWError as e:
                        ev = _evidence(page, out_dir, "after_submit_error")
                        return _record(db, job_id, app, actor, "SUBMISSION_UNVERIFIED", None, ev,
                                       f"browser error after clicking submit ({_first_line(e)}); outcome unknown")
                    if a.redirected_after_submit:
                        ev = _evidence(page, out_dir, "after_submit_redirect")
                        target = a.redirected_after_submit
                        return _record(db, job_id, app, actor, "SUBMISSION_UNVERIFIED", None,
                                       {**ev, "redirect": target},
                                       f"redirected to {host_of(target)} after submit (not followed); "
                                       "check your email for a confirmation")
                    res = a.result()
                    ev = _evidence(page, out_dir, "confirmation")
                    if res["kind"] == "success" and res["reference"]:
                        return _record(db, job_id, app, actor, "SUBMITTED", res["reference"], {**ev, **res}, None)
                    if res["kind"] == "success":
                        return _record(db, job_id, app, actor, "SUBMISSION_UNVERIFIED", None, {**ev, **res},
                                       "success message without a reference number")
                    if res["kind"] == "duplicate":
                        return _record(db, job_id, app, actor, "SUBMISSION_UNVERIFIED", None, {**ev, **res},
                                       "portal reports an existing application for this candidate")
                    if res["kind"] == "retryable_error":
                        db.upsert_application(job_id, app["idempotency_key"], retry_status="retrying")
                        raise TransientError("portal asked to try again: " + res["text"][:200])
                    if res["kind"] == "form_error":
                        db.upsert_application(job_id, app["idempotency_key"], retry_status=None)
                        raise NeedsHuman("portal rejected the form: " + res["text"][:300])
                    return _record(db, job_id, app, actor, "SUBMISSION_UNVERIFIED", None, {**ev, **res},
                                   "could not recognise the page after submit")
                except NeedsHuman as e:
                    _evidence(page, out_dir, "needs_human")
                    return _pause(db, job_id, app, actor, e.reason, e.questions)
                except FatalError as e:
                    _evidence(page, out_dir, "fatal")
                    return _fail(db, job_id, app, actor, str(e), closed=e.closed)
                except PWError as e:  # browser crash, detached element, ... anywhere outside the click itself
                    if (db.application(job_id)["retry_status"] or "") == "in_progress":
                        ev = _evidence(page, out_dir, "after_submit_error")
                        return _record(db, job_id, app, actor, "SUBMISSION_UNVERIFIED", None, ev,
                                       f"browser error after clicking submit ({_first_line(e)}); outcome unknown")
                    raise TransientError(f"browser error before submitting: {_first_line(e)}") from e
        except TransientError as e:
            last_error = str(e)
            db.audit(actor, "transient_error", "job", job_id, attempt=attempt, error=last_error)
            time.sleep(min(2 ** attempt * 0.25, 5))
            continue
    retry_at = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(microsecond=0).isoformat()
    db.upsert_application(job_id, app["idempotency_key"], last_error=last_error, retry_status="scheduled",
                          next_retry_at=retry_at)
    db.set_status(job_id, "FAILED", actor, f"gave up after {settings.max_apply_attempts} attempts: {last_error}")
    return {"outcome": "failed", "status": "FAILED", "detail": last_error, "next_retry_at": retry_at}


def dry_run(db: Database, settings: Settings, profile: Profile, job_id: int, actor: str = "candidate",
            headful: bool | None = None, hold: Callable[[], None] | None = None) -> dict:
    """Supervised rehearsal of an application on the real page. Never submits.

    The page is opened, checked, filled and screenshotted with the adapter in read-only mode: every
    request that could send data is aborted and the submit method refuses to run. The job's status is
    not changed. `hold` is called before the browser closes, so a headful run can be inspected.
    """
    job = db.job(job_id)
    if not job:
        raise LookupError(f"no job {job_id}")
    app = db.application(job_id)
    if not app or not app["resume_version_id"]:
        raise ValueError(f"job {job_id} has no prepared application yet: run `process` first")
    url = job["application_url"]
    key, apply_status, _ = capability_for(url)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out_dir = settings.documents_dir / f"job_{job_id}" / f"dry_run_{ts}"
    rep = {"job_id": job_id, "title": job["title"], "employer": job["employer"], "url": url, "source": key,
           "apply_status": apply_status, "at": ts, "submitted": False, "outcome": None, "detail": None,
           "fields": [], "open_questions": [], "invalid_fields": [], "blocked_requests": [], "evidence": {}}
    if apply_status == "assisted":
        rep.update(outcome="no_adapter", detail=ASSISTED_NOTE.format(site=CAPABILITIES[key].name))
        return _finish_dry_run(db, actor, rep, out_dir)
    if apply_status not in ("tested_local", "untested_live"):
        rep.update(outcome="no_adapter", detail=f"{key}: no automated adapter ({apply_status}); apply manually")
        return _finish_dry_run(db, actor, rep, out_dir)
    files = loads(db.resume_version(app["resume_version_id"])["files"], {})
    answers = loads(app["answers"], {})
    s = replace(settings, browser_headless=not headful) if headful is not None else settings
    with isolated_page(s) as page:
        a = GenericFormAdapter(page, allowed_domains_for(url, settings.extra_allowed_domains),
                               settings.browser_timeout_ms, read_only=True)
        try:
            _open_and_check(a, job)
            fields = a.fields()
            current = build_answers(fields, profile, dict(job), answers)
            new_q, unresolved = _open_questions(current, answers)
            rep["fields"] = _fill_form(a, fields, current, files, strict=False, resume_format=settings.upload_format)
            rep["open_questions"] = [{"question": q, "new": q in new_q, "note": current[q].get("note")}
                                     for q in sorted(set(new_q + unresolved))]
            rep["invalid_fields"] = a.invalid_required()
            ready = not (rep["open_questions"] or rep["invalid_fields"])
            rep["outcome"] = "ready_to_submit" if ready else "needs_input"
            rep["detail"] = ("every required field is filled; submit was NOT clicked" if ready else
                             "answer the open questions before this application can be submitted")
        except NeedsHuman as e:
            rep.update(outcome="needs_human", detail=e.reason)
        except FatalError as e:
            rep.update(outcome="closed" if e.closed else "blocked", detail=str(e))
        except TransientError as e:
            rep.update(outcome="transient_error", detail=str(e))
        rep["evidence"] = _evidence(page, out_dir, "dry_run")
        rep["blocked_requests"] = list(a.blocked_writes)
        if hold:
            hold()
    return _finish_dry_run(db, actor, rep, out_dir)


def _finish_dry_run(db, actor, rep: dict, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(dumps(rep))
    lines = [f"# Dry run: {rep['title']} at {rep['employer']}", "", "Nothing was submitted.", "",
             f"- URL: {rep['url']}", f"- Source: {rep['source']} ({rep['apply_status']})",
             f"- Outcome: **{rep['outcome']}**: {rep['detail']}",
             f"- Screenshot: {rep['evidence'].get('screenshot', 'none')}",
             f"- Requests blocked because they could send data: {len(rep['blocked_requests'])}", "",
             "| Field | Action | Value |", "|---|---|---|"]
    lines += [f"| {f['field']} | {f['action']} | {str(f['value'])[:80]} |" for f in rep["fields"]]
    if rep["open_questions"]:
        lines += ["", "## Open questions", ""] + [f"- {q['question']}" + (f" ({q['note']})" if q["note"] else "")
                                                  for q in rep["open_questions"]]
    (out_dir / "report.md").write_text("\n".join(lines) + "\n")
    rep["report"] = str(out_dir / "report.md")
    db.audit(actor, "dry_run", "job", rep["job_id"], outcome=rep["outcome"], detail=rep["detail"],
             report=rep["report"], blocked_requests=len(rep["blocked_requests"]), submitted=False)
    return rep


def _first_line(e: BaseException) -> str:
    return (str(e).strip().splitlines() or [type(e).__name__])[0][:200]


def _record(db, job_id, app, actor, status, reference, evidence, note) -> dict:
    verified = status == "SUBMITTED"
    db.upsert_application(job_id, app["idempotency_key"], submitted_at=now(), verified=int(verified),
                          verification=dumps({"evidence": evidence, "note": note}), reference=reference,
                          retry_status="done", blocker=None, last_error=note)
    db.set_status(job_id, status, actor, note or f"confirmation reference {reference}")
    db.audit(actor, "submission_recorded", "job", job_id, status=status, reference=reference, verified=verified,
             evidence={k: evidence.get(k) for k in ("screenshot", "url", "reference")}, note=note)
    return {"outcome": "submitted" if verified else "unverified", "status": status, "reference": reference,
            "detail": note, "evidence": evidence.get("screenshot")}


def _pause(db, job_id, app, actor, reason, questions=None) -> dict:
    db.upsert_application(job_id, app["idempotency_key"], blocker=reason, retry_status="paused_for_candidate",
                          approved_at=None, approved_by=None)
    db.set_status(job_id, "READY_FOR_REVIEW", actor, "paused for the candidate: " + reason, force=True)
    db.audit(actor, "paused_for_human", "job", job_id, reason=reason, questions=questions or [])
    return {"outcome": "needs_human", "status": "READY_FOR_REVIEW", "detail": reason, "questions": questions or []}


def _fail(db, job_id, app, actor, reason, closed=False) -> dict:
    db.upsert_application(job_id, app["idempotency_key"], last_error=reason, retry_status="not_retryable")
    db.set_status(job_id, "CLOSED" if closed else "FAILED", actor, reason)
    return {"outcome": "closed" if closed else "failed", "status": "CLOSED" if closed else "FAILED", "detail": reason}
