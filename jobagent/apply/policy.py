"""Application authorization: discovery, review and consented auto-apply modes.

These are deterministic rules. Nothing a job page or a model says can change them.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..db import Database, dumps, loads, now
from ..security import domain_allowed, host_of
from ..sources.base import CAPABILITIES

DEFAULT_AUTO_CONSTRAINTS = {
    "min_final_score": 70,
    "allowed_sources": [],            # e.g. ["greenhouse", "lever"]; empty = none
    "allowed_domains": [],            # application hosts the agent may submit on
    "allowed_apply_status": ["tested_local"],  # add "untested_live" only after a supervised live run
    "max_per_day": 5,
    "excluded_employers": [],
    "preapproved_declarations": [],   # question keys the candidate pre-approved (e.g. privacy consent)
}

CONSENT_STATEMENT = ("I, the candidate, authorize this agent to submit job applications on my behalf without asking "
                     "me first, only for jobs, sites and documents within the constraints recorded with this consent. "
                     "Applications with unknown facts, new questions, CAPTCHAs, logins or declarations I have not "
                     "pre-approved will still stop for my review. I can revoke this at any time.")


def capability_for(url: str | None) -> tuple[str, str, list[str]]:
    """(capability key, apply_status, legitimate domains) for an application URL."""
    if not url:
        return "unknown", "manual_only", []
    for key in ("testportal", "greenhouse", "lever", "ashby", "workday", "linkedin", "indeed"):
        cap = CAPABILITIES[key]
        if cap.domains and domain_allowed(url, cap.domains):
            return key, cap.apply_status, list(cap.domains)
    return "unknown", "manual_only", []


def allowed_domains_for(url: str, extra: list[str]) -> list[str]:
    key, _, domains = capability_for(url)
    host = host_of(url)
    if key == "testportal":
        return [host]  # only the exact local host, never "localhost" in general
    return sorted({host, *domains, *extra})


def grant_auto_consent(db: Database, constraints: dict, actor: str = "candidate") -> int:
    merged = {**DEFAULT_AUTO_CONSTRAINTS, **constraints}
    if not merged["allowed_sources"] or not merged["allowed_domains"]:
        raise ValueError("auto-apply consent needs explicit allowed_sources and allowed_domains")
    with db.tx() as c:
        cur = c.execute("INSERT INTO consents(kind, constraints, statement, granted_at) VALUES ('auto_apply',?,?,?)",
                        (dumps(merged), CONSENT_STATEMENT, now()))
        cid = int(cur.lastrowid)
    db.audit(actor, "auto_apply_consent_granted", "consent", cid, constraints=merged)
    return cid


def revoke_auto_consent(db: Database, actor: str = "candidate") -> None:
    with db.tx() as c:
        c.execute("UPDATE consents SET revoked_at=? WHERE kind='auto_apply' AND revoked_at IS NULL", (now(),))
    db.audit(actor, "auto_apply_consent_revoked", "consent", None)


@dataclass
class Decision:
    allowed: bool
    reason: str
    review_needed: bool = False   # route to the review checkpoint instead of failing


def blocking_answers(answers: dict) -> list[str]:
    """Required questions whose answer is not candidate-approved."""
    out = []
    for q, a in (answers or {}).items():
        if a.get("required") and a.get("status") not in ("answered", "file"):
            out.append(f"{q} ({a.get('status')})")
    return out


def authorize(db: Database, mode: str, job_id: int) -> Decision:
    job = db.job(job_id)
    app = db.application(job_id)
    if mode == "discovery":
        return Decision(False, "discovery mode never submits applications")
    if app is None:
        return Decision(False, "application has not been prepared")
    answers = loads(app["answers"], {})
    blockers = blocking_answers(answers)
    if mode == "review":
        if job["status"] not in ("APPROVED", "FAILED") or not app["approved_at"]:
            return Decision(False, "review mode: the candidate has not approved this application", review_needed=True)
        if blockers:
            return Decision(False, "unanswered required questions: " + "; ".join(blockers), review_needed=True)
        return Decision(True, f"approved by {app['approved_by']} at {app['approved_at']}")

    # ---- authorized auto-apply ----
    consent = db.active_consent("auto_apply")
    if consent is None:
        return Decision(False, "auto-apply has no active candidate consent", review_needed=True)
    c = loads(consent["constraints"], {})
    if job["status"] in ("APPROVED", "FAILED") and app["approved_at"]:
        if blockers:
            return Decision(False, "unanswered required questions: " + "; ".join(blockers), review_needed=True)
        return Decision(True, f"approved by {app['approved_by']}")
    url = job["application_url"] or ""
    key, apply_status, _ = capability_for(url)
    sources = {r["source"] for r in db.q("SELECT source FROM job_sources WHERE job_id=?", (job_id,))}
    final = db.latest_score(job_id, "final") or db.latest_score(job_id, "baseline")
    screen = loads(job["screen_result"], {})
    problems = []
    if apply_status not in c["allowed_apply_status"]:
        problems.append(f"adapter status {apply_status} for {key} not allowed by consent")
    if not sources & set(c["allowed_sources"]):
        problems.append(f"sources {sorted(sources)} not in consent {c['allowed_sources']}")
    if not domain_allowed(url, c["allowed_domains"]):
        problems.append(f"domain {host_of(url)} not in consent")
    if final is None or final["total"] < c["min_final_score"]:
        problems.append(f"final score {final['total'] if final else None} below consent minimum {c['min_final_score']}")
    if job["employer"] in c.get("excluded_employers", []):
        problems.append("employer excluded by consent")
    if screen.get("unknowns"):
        problems.append("material unknowns: " + "; ".join(screen["unknowns"]))
    if blockers:
        problems.append("answers need the candidate: " + "; ".join(blockers))
    since = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat()
    today_count = db.one("SELECT COUNT(*) n FROM applications WHERE submitted_at >= ?", (since,))["n"]
    if today_count >= c["max_per_day"]:
        problems.append(f"daily limit {c['max_per_day']} reached")
    if problems:
        return Decision(False, "; ".join(problems), review_needed=True)
    return Decision(True, f"within auto-apply consent #{consent['id']}")
