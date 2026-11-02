"""Assisted applications for LinkedIn, Indeed and Naukri.com: the candidate submits, the agent prepares.

The agent never fills or submits forms on these sites. It builds an "application kit" (the tailored
resume and cover letter, plus answers to the questions these sites usually ask, each taken from the
candidate's own verified profile), opens the job in the candidate's normal browser, and records the
submission only when the candidate says they made it.
"""
from __future__ import annotations

from pathlib import Path

from ..answers import answer as answer_question
from ..db import Database, dumps, loads, now
from ..profile import Profile
from ..sources.base import CAPABILITIES
from ..sources.jobsites import SITE_NAMES
from .policy import capability_for

# Questions these sites commonly ask (Naukri's apply questionnaire, LinkedIn Easy Apply, Indeed Apply).
COMMON_QUESTIONS = {
    "all": ["First name", "Last name", "Email", "Phone", "Current location", "Total years of experience",
            "Current company", "Current designation", "Notice period", "Expected salary",
            "Are you willing to relocate?", "Are you legally authorized to work in this country?",
            "Will you now or in the future require visa sponsorship?", "LinkedIn profile", "Highest education"],
    "naukri": ["Current CTC", "Expected CTC"],
    "linkedin": [],
    "indeed": [],
}
DONE = ("SUBMITTED", "SUBMISSION_UNVERIFIED", "INTERVIEW", "OFFER")


def is_assisted(job) -> bool:
    return capability_for(job["application_url"] or job["original_url"])[1] == "assisted"


def kit(db: Database, profile: Profile, job_id: int) -> dict:
    """Everything the candidate needs to apply by hand. Pure: writes nothing."""
    job = db.job(job_id)
    if job is None:
        raise LookupError(f"no job {job_id}")
    url = job["application_url"] or job["original_url"]
    site, status, _ = capability_for(url)
    app = db.application(job_id)
    files = {}
    if app and app["resume_version_id"]:
        files = {k: v for k, v in loads(db.resume_version(app["resume_version_id"])["files"], {}).items() if v}
    stored = loads(app["answers"], {}) if app else {}
    questions = list(dict.fromkeys(COMMON_QUESTIONS["all"] + COMMON_QUESTIONS.get(site, [])))
    answers = []
    for q in questions:
        a = answer_question(q, profile, dict(job), None)
        answers.append({"question": q, "value": a.value, "status": a.status, "source": a.source, "note": a.note})
    for q, a in stored.items():  # answers the candidate already gave for this job win
        if a.get("source") == "candidate":
            answers = [x for x in answers if x["question"] != q] + [
                {"question": q, "value": a.get("value"), "status": "answered", "source": "candidate", "note": ""}]
    return {
        "job_id": job_id, "title": job["title"], "employer": job["employer"], "url": url, "site": site,
        "site_name": SITE_NAMES.get(site) or CAPABILITIES.get(site, CAPABILITIES["manual"]).name,
        "assisted": status == "assisted", "status": job["status"],
        "files": files, "cover_letter": (app["cover_letter"] if app else None) or "",
        "answers": answers, "prepared": bool(files),
        "submitted": job["status"] in DONE or bool(app and app["submitted_at"]),
        "steps": [
            f"Open the job on {SITE_NAMES.get(site, 'the site')} in your own browser, signed in as yourself.",
            "Check it is still open and is the same role and employer.",
            "Upload the tailored resume below (not your old one) and paste the cover letter if asked.",
            "Answer the questions yourself. The answers below come from your profile; anything marked "
            "'needs you' is not guessed.",
            "Read everything once more and click submit yourself.",
            "Come back and mark it as submitted, with the confirmation reference if the site shows one.",
        ],
    }


def write_kit(db: Database, documents_dir: Path, profile: Profile, job_id: int) -> tuple[dict, Path]:
    k = kit(db, profile, job_id)
    out = documents_dir / f"job_{job_id}"
    out.mkdir(parents=True, exist_ok=True)
    lines = [f"# Apply yourself: {k['title']} at {k['employer']}", "",
             f"Site: {k['site_name']} (applications here are never automated)", f"Job link: {k['url']}", "",
             "## Steps", ""] + [f"{i}. {s}" for i, s in enumerate(k["steps"], 1)] + ["", "## Files", ""]
    lines += [f"- {kind}: {path}" for kind, path in k["files"].items()] or [
        "- none yet: run `jobagent process` to prepare the tailored resume"]
    lines += ["", "## Answers from your profile", "", "| Question | Answer | Status |", "|---|---|---|"]
    for a in k["answers"]:
        val = a["value"] if a["value"] not in (None, "") else ""
        state = "ok" if a["status"] == "answered" else "needs you" + (f": {a['note']}" if a["note"] else "")
        lines.append(f"| {a['question']} | {str(val).replace('|', '/')} | {state} |")
    if k["cover_letter"]:
        lines += ["", "## Cover letter", "", k["cover_letter"]]
    path = out / "apply_kit.md"
    path.write_text("\n".join(lines) + "\n")
    db.audit("agent", "assist_kit_prepared", "job", job_id, site=k["site"], kit=str(path))
    return k, path


def record_manual_submission(db: Database, job_id: int, reference: str | None = None,
                             actor: str = "candidate", note: str | None = None) -> dict:
    """The candidate says they submitted this application themselves. Recorded as SUBMITTED with
    verified=0: the agent did not see a confirmation page, the candidate's word is the evidence."""
    job = db.job(job_id)
    if job is None:
        raise LookupError(f"no job {job_id}")
    app = db.application(job_id)
    if job["status"] in DONE or (app and app["submitted_at"]):
        return {"outcome": "already_recorded", "status": job["status"]}
    key = app["idempotency_key"] if app else f"manual:{job['dedup_key']}"
    detail = note or f"submitted by the candidate on {job['application_url'] or job['original_url']}"
    db.upsert_application(job_id, key, submitted_at=now(), verified=0, reference=(reference or None),
                          verification=dumps({"by": actor, "note": detail}), retry_status="done", blocker=None)
    db.set_status(job_id, "SUBMITTED", actor, detail + (f"; reference {reference}" if reference else ""), force=True)
    db.audit(actor, "manual_submission_recorded", "job", job_id, reference=reference, note=detail)
    return {"outcome": "recorded", "status": "SUBMITTED", "reference": reference}
