"""Orchestration: discover -> screen -> score -> optimize -> prepare -> (approve) -> apply -> retry."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from .apply import engine
from .apply.adapters import ApplyError, FatalError, NeedsHuman, TransientError
from .apply.policy import blocking_answers, capability_for
from .config import Settings
from .db import RECOVERABLE, Database, dumps, loads, now
from .documents import template_cover_letter, write_version
from .filtering import screen
from .http import PoliteClient
from .llm import draft_cover_letter, get_llm
from .normalize import upsert_job
from .optimizer import TruthViolation, optimize
from .profile import Profile
from .requirements import parse_job
from .resume_doc import ResumeDoc, master_resume
from .sources.registry import build_sources, discover


def job_reqs(job):
    return parse_job(job["description"] or "", loads(job["required_qualifications"], []),
                     loads(job["preferred_qualifications"], []))


def run_discovery(db: Database, settings: Settings, source_configs: list[dict] | None = None) -> dict:
    http = PoliteClient(settings.user_agent, settings.http_timeout, settings.http_retries, settings.per_host_min_interval)
    try:
        sources = build_sources(source_configs if source_configs is not None else settings.sources, http)
        records, errors = discover(sources)
    finally:
        http.close()
    notes = source_notes(sources)
    created = merged = 0
    for rec in records:
        _, is_new = upsert_job(db, rec)
        created += is_new
        merged += not is_new
    for e in errors:
        db.audit("discovery", "source_error", None, None, **e)
    for n in notes:
        db.audit("discovery", "source_note", None, None, **n)
    out = {"fetched": len(records), "new_jobs": created, "merged_duplicates": merged, "errors": errors}
    if notes:
        out["notes"] = notes
    return out


def source_notes(sources) -> list[dict]:
    """Things a source skipped that the candidate should know: saved pages that were a login wall or
    CAPTCHA, and queued job URLs whose page has not been saved yet."""
    out = []
    for s in sources:
        for p in getattr(s, "problems", []):
            out.append({"source": s.name, "note": f"skipped {p}"})
        pending = getattr(s, "pending", [])
        if pending:
            out.append({"source": s.name, "note": f"{len(pending)} queued job URL(s) have no saved page yet "
                        "(save them from your browser, or run `jobagent capture`)",
                        "urls": [r.url for r in pending]})
    return out


def _master_version(db: Database, settings: Settings, profile: Profile) -> tuple[int, ResumeDoc]:
    doc = master_resume(profile)
    sha = doc.sha256()
    row = db.one("SELECT id FROM resume_versions WHERE job_id IS NULL AND sha256=?", (sha,))
    if row:
        return row["id"], doc
    files = write_version(doc, settings.documents_dir / "master", f"master_{sha[:8]}")
    rv = db.add_resume_version(None, "master", doc.to_dict(), [], files, sha)
    db.audit("system", "master_resume_version", "resume_version", rv, sha=sha)
    return rv, doc


def screen_and_score(db: Database, settings: Settings, profile: Profile, job_ids: list[int] | None = None) -> dict:
    # New jobs, plus jobs whose preparation failed or stopped part-way and that have no application yet
    stuck = ",".join(f"'{s}'" for s in RECOVERABLE)
    rows = db.q("SELECT * FROM jobs WHERE status='DISCOVERED' OR (id NOT IN (SELECT job_id FROM applications) AND "
                f"(status IN ({stuck}) OR (status='SCREENED' AND json_extract(screen_result, '$.passed') = 1))) "
                "ORDER BY id" if job_ids is None else
                f"SELECT * FROM jobs WHERE id IN ({','.join('?' * len(job_ids))})", tuple(job_ids or ()))
    master_id, master = _master_version(db, settings, profile)
    stats = {"screened": 0, "passed": 0, "scored": 0, "optimized": 0, "ready": 0, "closed": 0, "errors": []}
    llm = get_llm(settings)
    for job in rows:
        retry = job["status"] != "DISCOVERED"
        if retry and (job["status"] not in (*RECOVERABLE, "SCREENED") or db.application(job["id"]) is not None
                      or (job["status"] == "SCREENED" and not loads(job["screen_result"], {}).get("passed"))):
            continue
        try:
            _screen_one(db, settings, profile, job, retry, master_id, master, llm, stats)
        except TruthViolation as e:
            stats["errors"].append({"job": job["id"], "error": str(e)})
            db.audit("optimizer", "truth_guard_blocked", "job", job["id"], violations=[str(v) for v in e.violations])
            _preparation_failed(db, job["id"], f"truth guard blocked the tailored resume: {e}")
        except Exception as e:  # one bad job must not stop the batch or strand the job
            err = f"{type(e).__name__}: {e}"
            stats["errors"].append({"job": job["id"], "error": err})
            db.audit("pipeline", "preparation_error", "job", job["id"], error=err)
            _preparation_failed(db, job["id"], err)
    return stats


def _screen_one(db, settings, profile, job, retry, master_id, master, llm, stats) -> None:
    reqs = job_reqs(job)
    jd = dict(job)
    src_rows = db.q("SELECT source, raw FROM job_sources WHERE job_id=?", (job["id"],))
    jd["sources"] = [r["source"] for r in src_rows]
    raws = [loads(r["raw"], {}) or {} for r in src_rows]
    for k in ("salary_from_text", "employment_type_from_text"):
        jd[k] = any(x.get(k) for x in raws)
    res = screen(jd, reqs, profile, relevance_threshold=settings.relevance_threshold,
                 max_age_days=settings.max_job_age_days)
    flags = []
    if res.fraud_signals:
        flags.append("possible_fraud")
    if res.injection:
        flags.append("prompt_injection_text")
    if res.expired:
        flags.append("expired")
    db.update_job(job["id"], screen_result=res.to_dict(), flags=flags)
    db.set_status(job["id"], "SCREENED", "screening", "passed" if res.passed else "; ".join(res.disqualifiers),
                  force=retry and job["status"] != "PREPARATION_FAILED")
    stats["screened"] += 1
    if res.expired:
        db.set_status(job["id"], "CLOSED", "screening", "expired listing")
        stats["closed"] += 1
        return
    if not res.passed:
        return
    stats["passed"] += 1
    _score_and_prepare(db, settings, profile, db.job(job["id"]), reqs, master_id, master,
                       llm if not res.injection else None, stats)


def _preparation_failed(db: Database, job_id: int, reason: str) -> None:
    """Park a job whose preparation failed where the next run retries it (unless it was closed meanwhile)."""
    if db.job(job_id)["status"] in ("SCREENED", "SCORED", "OPTIMIZATION_REQUIRED", "OPTIMIZED"):
        db.set_status(job_id, "PREPARATION_FAILED", "pipeline", reason[:500])


def _score_and_prepare(db, settings, profile, job, reqs, master_id, master, llm, stats) -> None:
    jid = job["id"]
    res = optimize(profile, master, reqs, job["title"], weights=settings.weights, optimize_below=settings.optimize_below,
                   target_min=settings.target_min, target_max=settings.target_max, llm=llm)
    db.add_score(jid, "baseline", res.baseline.total, res.baseline.to_dict(), master_id)
    db.set_status(jid, "SCORED", "scoring", f"baseline {res.baseline.total}")
    stats["scored"] += 1
    if res.outcome != "not_required":
        db.set_status(jid, "OPTIMIZATION_REQUIRED", "scoring", f"baseline {res.baseline.total} < {settings.optimize_below}")
    out_dir = settings.documents_dir / f"job_{jid}"
    count = db.one("SELECT COUNT(*) n FROM resume_versions WHERE job_id=?", (jid,))["n"]
    stem = f"resume_job{jid}_v{count + 1}"
    files = write_version(res.doc, out_dir, stem, pdf=settings.pdf_output)
    letter, letter_src = None, "template"
    if llm is not None:
        letter, problems = draft_cover_letter(llm, profile, res.doc, dict(job), reqs)
        if problems:
            db.audit("llm", "cover_letter_rejected", "job", jid, problems=problems)
        letter_src = "model (passed truth guard)" if letter else "template"
    if letter is None:
        letter = template_cover_letter(profile, res.doc, dict(job), reqs)
    files["cover_letter_txt"] = str(out_dir / f"cover_letter_job{jid}_v{count + 1}.txt")
    Path(files["cover_letter_txt"]).write_text(letter)
    import docx
    d = docx.Document()
    for para in letter.split("\n"):
        d.add_paragraph(para)
    files["cover_letter_docx"] = str(out_dir / f"cover_letter_job{jid}_v{count + 1}.docx")
    d.save(files["cover_letter_docx"])
    report = res.report()
    report["cover_letter_source"] = letter_src
    rv = db.add_resume_version(jid, f"job{jid}_v{count + 1}", res.doc.to_dict(), report, files, res.doc.sha256())
    db.add_score(jid, "final", res.final.total, res.final.to_dict(), rv)
    db.audit("optimizer", "resume_version_created", "job", jid, resume_version=rv, baseline=res.baseline.total,
             final=res.final.total, outcome=res.outcome, changes=len(res.changes), rejected=len(res.rejected))
    if res.outcome != "not_required":
        db.set_status(jid, "OPTIMIZED", "optimizer", f"{res.outcome}: {res.baseline.total} -> {res.final.total}")
        stats["optimized"] += 1

    key = hashlib.sha256(f"{job['dedup_key']}|{(profile.contact.email or '').lower()}".encode()).hexdigest()
    existing = db.application(jid)
    answers = loads(existing["answers"], {}) if existing else {}
    screen_res = loads(job["screen_result"], {})
    blocker, closed, paused = None, False, None
    try:
        fields = engine.inspect_form(settings, job)
        answers = engine.build_answers(fields, profile, dict(job), answers)
    except NeedsHuman as e:
        blocker, paused = e.reason, "paused_for_candidate"
    except FatalError as e:
        blocker, closed = f"application page problem: {e}", e.closed
    except (TransientError, ApplyError) as e:
        blocker = f"could not inspect the application form: {e}"
    except Exception as e:  # browser not installed etc.: preparation still succeeds
        blocker = f"form inspection unavailable: {type(e).__name__}: {e}"
    unknowns = screen_res.get("unknowns", []) + blocking_answers(answers)
    db.upsert_application(jid, key, resume_version_id=rv, cover_letter=letter, answers=answers, unknowns=unknowns,
                          blocker=blocker, retry_status=paused)
    if closed:
        db.set_status(jid, "CLOSED", "preparation", blocker)
        stats["closed"] += 1
        return
    db.set_status(jid, "READY_FOR_REVIEW", "preparation", blocker or "documents and answers prepared")
    stats["ready"] += 1


def set_answer(db: Database, job_id: int, question: str, value: str, actor: str = "candidate") -> None:
    app = db.application(job_id)
    if app is None:
        raise LookupError("application not prepared")
    answers = loads(app["answers"], {})
    prev = answers.get(question, {"required": True, "type": "text", "options": []})
    answers[question] = {**prev, "value": value, "status": "answered", "source": "candidate", "note": "provided by candidate"}
    unknowns = [u for u in loads(app["unknowns"], []) if not u.startswith(question)]
    db.upsert_application(job_id, app["idempotency_key"], answers=answers, unknowns=unknowns)
    db.audit(actor, "answer_set", "job", job_id, question=question)


def approve(db: Database, job_id: int, actor: str = "candidate") -> None:
    job, app = db.job(job_id), db.application(job_id)
    if app is None or job["status"] != "READY_FOR_REVIEW":
        raise ValueError(f"job {job_id} is {job['status']}; only READY_FOR_REVIEW applications can be approved")
    blockers = blocking_answers(loads(app["answers"], {}))
    if blockers:
        raise ValueError("answer these first (jobagent answer JOB_ID QUESTION VALUE): " + "; ".join(blockers))
    db.upsert_application(job_id, app["idempotency_key"], approved_at=now(), approved_by=actor, blocker=None,
                          retry_status=None)
    db.set_status(job_id, "APPROVED", actor, "candidate approved documents and answers")


def apply_pending(db: Database, settings: Settings, profile: Profile, job_ids: list[int] | None = None) -> list[dict]:
    if settings.mode == "discovery":
        return []
    statuses = ("APPROVED",) if settings.mode == "review" else ("APPROVED", "READY_FOR_REVIEW")
    rows = db.q(f"SELECT id FROM jobs WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY id", statuses)
    results = []
    for r in rows:
        if job_ids and r["id"] not in job_ids:
            continue
        app = db.application(r["id"])
        if settings.mode == "auto" and db.job(r["id"])["status"] == "READY_FOR_REVIEW" and app \
                and app["retry_status"] == "paused_for_candidate":
            # paused applications wait for the candidate even in auto mode
            continue
        out = engine.submit(db, settings, profile, r["id"])
        results.append({"job_id": r["id"], **out})
    return results


def retry_failed(db: Database, settings: Settings, profile: Profile) -> list[dict]:
    t = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    rows = db.q("SELECT j.id FROM jobs j JOIN applications a ON a.job_id=j.id WHERE j.status='FAILED' "
                "AND a.retry_status='scheduled' AND a.next_retry_at <= ?", (t,))
    return [{"job_id": r["id"], **engine.submit(db, settings, profile, r["id"], actor="retry")} for r in rows]


def source_label(cfg: dict) -> str:
    key = cfg.get("board_token") or cfg.get("site") or cfg.get("organization") or cfg.get("tenant") \
        or cfg.get("url") or ", ".join(cfg.get("urls", [])) or cfg.get("path") or cfg.get("folder") \
        or (cfg.get("imap") or {}).get("user")
    return f"{cfg['type']}:{key}" + (f" ({cfg['employer']})" if cfg.get("employer") else "")


def check_sources(settings: Settings) -> list[dict]:
    """Fetch every configured source once, without storing anything. One result per source."""
    http = PoliteClient(settings.user_agent, settings.http_timeout, settings.http_retries, settings.per_host_min_interval)
    out = []
    try:
        for cfg, src in zip(settings.sources, build_sources(settings.sources, http)):
            try:
                jobs = list(src.fetch())
                out.append({"source": source_label(cfg), "ok": True, "jobs": len(jobs),
                            "sample": [{"title": j.title, "location": j.location, "url": j.application_url}
                                       for j in jobs[:3]],
                            "notes": [n["note"] for n in source_notes([src])]})
            except Exception as e:
                out.append({"source": source_label(cfg), "ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"})
    finally:
        http.close()
    return out


def capability_report() -> list[dict]:
    from .sources.base import CAPABILITIES
    return [{"key": k, **c.__dict__} for k, c in CAPABILITIES.items()]


__all__ = ["run_discovery", "screen_and_score", "approve", "set_answer", "apply_pending", "retry_failed",
           "capability_for", "capability_report", "dumps"]
