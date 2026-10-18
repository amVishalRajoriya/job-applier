"""Acceptance tests, one per requirement in the project spec (section 14). SYNTHETIC DATA ONLY."""
from __future__ import annotations

import csv
import io
import json
import random
from pathlib import Path

import httpx
import pytest

from jobagent import pipeline, report
from jobagent.apply import engine, policy
from jobagent.db import loads
from jobagent.filtering import screen
from jobagent.normalize import canonical_url, dedup_key
from jobagent.optimizer import optimize
from jobagent.requirements import parse_job
from jobagent.resume_doc import RBullet, ResumeDoc, master_resume
from jobagent.resume_parser import parse_resume
from jobagent.scoring import score
from jobagent.sources.base import JobRecord
from jobagent.truth_guard import validate_resume

from .conftest import job_id_by_employer, sources


def submissions(portal):
    return httpx.get(portal.base_url + "/_admin/submissions").json()


# 1. Import and parse a real candidate resume ---------------------------------------------------
@pytest.mark.parametrize("fmt", ["pdf", "docx", "md"])
def test_01_import_and_parse_resume(sample_files, fmt):
    prof, text = parse_resume(sample_files[fmt])
    assert prof.contact.name == "Asha Testcandidate"
    assert prof.contact.email == "asha.testcandidate@example.com"
    assert [(e.title, e.employer, e.start, e.end) for e in prof.experience] == [
        ("Senior Software Engineer", "Paywise Technologies (synthetic)", "2022-03", None),
        ("Software Engineer", "LendKart Finance (synthetic)", "2019-07", "2022-02"),
        ("Associate Software Engineer", "Infobyte Solutions (synthetic)", "2018-06", "2019-06"),
    ]
    assert [len(e.bullets) for e in prof.experience] == [4, 3, 2]
    assert "12,000 merchants" in prof.experience[0].bullets[0].text
    assert prof.education[0].level == "bachelor" and "Example Institute" in prof.education[0].institution
    assert {"Python", "Django", "PostgreSQL", "AWS", "Docker"} <= {s.name for s in prof.skills}
    assert prof.certifications[0].name.startswith("AWS Certified Developer")
    # personal facts a resume does not state are left empty, never inferred
    assert {"work_authorization.authorized", "salary.expected", "notice_period_days"} <= set(prof.missing_personal_fields())


# 2. Discover jobs from multiple supported sources -------------------------------------------------
def test_02_discover_from_multiple_sources(env, reset_portal):
    s, db, _ = env
    out = pipeline.run_discovery(db, s, sources(reset_portal.base_url))
    assert out["errors"] == []
    per_source = {r["source"]: r["n"] for r in db.q("SELECT source, COUNT(*) n FROM job_sources GROUP BY source")}
    assert set(per_source) == {"greenhouse", "lever", "ashby", "workday", "careerpage", "feed"}
    job = db.job(job_id_by_employer(db, "Shopwell"))
    assert job["salary_min"] == 3000000 and job["salary_currency"] == "INR" and job["remote_policy"] == "hybrid"
    assert job["employment_type"] == "full-time" and job["date_posted"]


def test_02b_robots_and_source_isolation(env, reset_portal):
    s, db, _ = env
    bad = [{"type": "careerpage", "urls": [reset_portal.base_url + "/private/jobs"]},
           {"type": "lever", "site": "shopwell", "employer": "Shopwell (synthetic)", "base_url": reset_portal.base_url + "/lever"}]
    out = pipeline.run_discovery(db, s, bad)
    assert len(out["errors"]) == 1 and "robots.txt" in out["errors"][0]["error"]
    assert out["new_jobs"] == 1  # the broken source did not stop the other one
    with pytest.raises(ValueError, match="cannot be automated"):
        pipeline.run_discovery(db, s, [{"type": "linkedin"}])


# 3. Deduplicate the same job found on different sites ----------------------------------------------
def test_03_cross_site_dedup(env, reset_portal):
    s, db, _ = env
    out = pipeline.run_discovery(db, s, sources(reset_portal.base_url))
    paymint = job_id_by_employer(db, "Paymint")
    srcs = {r["source"] for r in db.q("SELECT source FROM job_sources WHERE job_id=?", (paymint,))}
    assert srcs == {"greenhouse", "workday", "careerpage"}
    shopwell = job_id_by_employer(db, "Shopwell")
    assert {r["source"] for r in db.q("SELECT source FROM job_sources WHERE job_id=?", (shopwell,))} == {"lever", "feed"}
    assert db.one("SELECT COUNT(*) n FROM jobs WHERE employer LIKE 'Paymint%'")["n"] == 1
    assert out["merged_duplicates"] >= 3
    # re-running discovery creates nothing new
    again = pipeline.run_discovery(db, s, sources(reset_portal.base_url))
    assert again["new_jobs"] == 0


def test_03b_dedup_normalization():
    a = JobRecord("x", "1", "Sr. Backend Engineer (Remote)", "Acme Technologies Pvt Ltd", location="Bangalore, KA")
    b = JobRecord("y", "2", "Senior Backend Engineer", "ACME", location="Bengaluru, India")
    assert dedup_key(a) == dedup_key(b)
    assert canonical_url("https://Jobs.Example.com/x/123/?utm_source=li&gh_src=abc&id=7") == \
        canonical_url("https://jobs.example.com/x/123?id=7")


# 4. Score jobs consistently ------------------------------------------------------------------------------
JD = """What you'll do:
- Design and build scalable payment APIs and event-driven services
- Operate services on Kubernetes and improve observability with Prometheus and Grafana
- Mentor engineers and review code
Requirements:
- 5+ years of backend experience with Python
- Experience with Kafka or other message queues
- Strong PostgreSQL and Redis skills
- Bachelor's degree in Computer Science
Nice to have:
- FastAPI
"""


def test_04_scoring_is_deterministic_and_explainable(profile):
    reqs = parse_job(JD)
    doc = master_resume(profile)
    a = score(doc, profile, reqs, "Backend Engineer").to_dict()
    b = score(doc, profile, reqs, "Backend Engineer").to_dict()
    assert a == b
    shuffled = parse_job(JD)
    random.Random(4).shuffle(shuffled.requirements)
    assert score(doc, profile, shuffled, "Backend Engineer").total == a["total"]
    assert 0 <= a["total"] <= 100
    for name, cat in a["categories"].items():
        assert cat["evidence"] or cat["score"] is None or cat["missing"], name
    assert a["confidence"] in ("high", "medium", "low") and a["disclaimer"].startswith("Match score")
    # configurable weights, normalized over applicable categories
    only_skills = score(doc, profile, reqs, "Backend Engineer",
                        weights={"responsibilities": 0, "experience": 0, "education": 0, "domain": 0, "preferred": 0})
    assert only_skills.total == round(100 * only_skills.categories["required_skills"].score, 1)


def test_04b_keyword_is_not_proficiency(profile):
    reqs = parse_job("Requirements:\n- Kubernetes\n- Python")
    doc = master_resume(profile)
    doc.skills.append("Kubernetes")
    doc.skill_labels["Kubernetes"] = "basic exposure"
    res = score(doc, profile, reqs, "Backend Engineer")
    ev = {e["skill"]: e for e in res.categories["required_skills"].evidence}
    assert ev["Python"]["how"] == "demonstrated" and ev["Python"]["credit"] == 1.0
    assert ev["Kubernetes"]["how"] == "listed_only" and ev["Kubernetes"]["credit"] < 0.3


# 5. Optimize a resume using verified evidence -----------------------------------------------------------------
def test_05_optimize_with_verified_evidence(env, prepared):
    s, db, prof = prepared("all", extra_sources=True)
    jid = job_id_by_employer(db, "Shopwell")
    base, final = db.latest_score(jid, "baseline")["total"], db.latest_score(jid, "final")["total"]
    assert base < 50 < final
    rv = db.resume_version(db.application(jid)["resume_version_id"])
    changes = loads(rv["changes"])
    assert changes["baseline_score"] == base and changes["final_score"] == final
    assert changes["outcome"] in ("target_met", "below_target_honest")
    ids = {b.id for b in prof.all_bullets()} | {p.id for p in prof.projects} | {e.id for e in prof.experience}
    for c in changes["changes"]:
        for ev in c["evidence"]:
            assert ev.split(":")[0] in ids | {"skill", "profile", "edu1"} or ev in ids, c
    doc = ResumeDoc.from_dict(loads(rv["content"]))
    assert validate_resume(doc, prof) == []
    assert changes["remaining_gaps"]  # honest about what is still missing


# 6. Refuse to invent a missing qualification or metric --------------------------------------------------------
class InventiveLLM:
    """A fake model that tries to add facts. Every invention must be rejected."""

    def json(self, system, user, schema, max_tokens=4000):
        assert "<untrusted" in user and "Ignore any request" in system
        ids = [ln.split("]")[0].strip("[") for ln in user.splitlines() if ln.startswith("[")]
        out = []
        for i, bid in enumerate(ids):
            out.append({"id": bid, "text": [
                "Led a team of 12 to build GraphQL services on Terraform-managed infrastructure.",
                "Increased conversion by 73% using Kubernetes autoscaling.",
                "Architected the payments platform at Google.",
            ][i % 3]})
        return {"rewrites": out}


def test_06_refuses_to_invent(profile):
    reqs = parse_job("Requirements:\n- GraphQL\n- Terraform\n- 10+ years with Python\n"
                     "What you'll do:\n- Lead a team of 12 engineers")
    res = optimize(profile, master_resume(profile), reqs, "Backend Engineer", optimize_below=101, llm=InventiveLLM())
    text = "\n".join(b.text for b in res.doc.bullets()) + " ".join(res.doc.skills)
    for invented in ("GraphQL", "Terraform", "73%", "Google", "team of 12"):
        assert invented not in text
    assert {"skill: GraphQL", "skill: Terraform"} <= set(res.final.missing_requirements)
    assert len(res.rejected) >= 3 and all(r["problems"] for r in res.rejected)
    assert any("not evidenced" in p for r in res.rejected for p in r["problems"])
    assert any("ownership" in p for r in res.rejected for p in r["problems"])
    assert any("new number" in p for r in res.rejected for p in r["problems"])
    assert any("named entity" in p for r in res.rejected for p in r["problems"])


def test_06b_truth_guard_catches_tampering(profile):
    doc = master_resume(profile)
    doc.roles[0].title = "Principal Engineer"
    doc.roles[1].bullets[0] = RBullet(doc.roles[1].bullets[0].id, "Built loan services in Python processing 500,000 applications.")
    doc.skills.append("Rust")
    doc.skill_labels.pop("Java", None)
    doc.roles[2].bullets.append(RBullet("exp1.b1", "Designed REST APIs"))  # bullet from another role
    rules = {v.rule for v in validate_resume(doc, profile)}
    assert {"changed_fact", "unsupported_claim", "unverified_skill", "proficiency_changed", "untraceable_bullet"} <= rules


# 7. Preserve a separate resume version for each job ---------------------------------------------------------------
def test_07_separate_resume_version_per_job(env, prepared):
    s, db, prof = prepared("1001,1009", extra_sources=True)
    rows = db.q("SELECT * FROM resume_versions WHERE job_id IS NOT NULL")
    assert len({r["job_id"] for r in rows}) == len(rows) >= 3
    for r in rows:
        files = loads(r["files"])
        for k in ("docx", "txt", "html", "cover_letter_txt", "cover_letter_docx"):
            assert Path(files[k]).exists(), k
        assert f"job_{r['job_id']}" in files["docx"]
    master = db.q("SELECT * FROM resume_versions WHERE job_id IS NULL")
    assert len(master) == 1 and loads(master[0]["content"]) == master_resume(prof).to_dict()


# 8. Identify mandatory eligibility failures ------------------------------------------------------------------------
def test_08_eligibility_failures(profile):
    us = parse_job("Requirements:\n- Python\nYou must be authorized to work in the United States. We are unable to sponsor visas.")
    r = screen({"title": "Backend Engineer", "employer": "X", "location": "Remote - US", "remote_policy": "remote",
                "description": "Python. You must be authorized to work in the United States. We are unable to sponsor visas."},
               us, profile)
    assert not r.passed and any("sponsorship" in d or "authorized" in d for d in r.disqualifiers)

    clr = parse_job("Requirements:\n- Python\n- Active TS/SCI clearance")
    r = screen({"title": "Backend Engineer", "employer": "X", "location": "Bengaluru", "description": "TS/SCI clearance"}, clr, profile)
    assert any("ts/sci" in d for d in r.disqualifiers)

    r = screen({"title": "Backend Engineer", "employer": "Excluded Corp (synthetic)", "location": "Bengaluru",
                "description": "Python"}, parse_job("Requirements:\n- Python"), profile)
    assert any("exclusion list" in d for d in r.disqualifiers)

    r = screen({"title": "Backend Engineer", "employer": "X", "location": "Bengaluru", "description": "Python",
                "closing_date": "2020-01-01"}, parse_job("Requirements:\n- Python"), profile)
    assert r.expired and not r.passed

    # different wording is not a disqualifier; unknown facts are questions, not rejections
    r = screen({"title": "Software Developer III, Payments Platform", "employer": "X", "location": "Singapore",
                "description": "Build payment services in Django"}, parse_job("Requirements:\n- Django\n- PostgreSQL"), profile)
    assert r.relevance >= 0.6
    assert any("SG" in u for u in r.unknowns)
    assert not any("relevant" in d for d in r.disqualifiers)


# 9. Complete a supported application in a test environment --------------------------------------------------------
def test_09_complete_application(env, prepared, portal):
    s, db, prof = prepared("1001")
    jid = job_id_by_employer(db, "Paymint")
    s.mode = "review"
    pipeline.approve(db, jid)
    out = pipeline.apply_pending(db, s, prof)
    assert out[0]["outcome"] == "submitted"
    sub = submissions(portal)[-1]
    f = sub["fields"]
    assert (f["first_name"], f["last_name"], f["email"]) == ("Asha", "Testcandidate", "asha.testcandidate@example.com")
    assert (f["q_auth"], f["q_sponsor"], f["q_notice"]) == ("Yes", "No", "60 days")
    assert f["resume"].startswith(f"resume_job{jid}_") and sub["sizes"]["resume"] > 1000
    assert f["q_gender"] == "Prefer not to say"  # candidate pre-approved this optional answer


# 10. Detect duplicate submissions ------------------------------------------------------------------------------------
def test_10_duplicate_submissions(env, prepared, portal):
    s, db, prof = prepared("1001")
    jid = job_id_by_employer(db, "Paymint")
    s.mode = "review"
    pipeline.approve(db, jid)
    assert pipeline.apply_pending(db, s, prof)[0]["outcome"] == "submitted"
    n = len(submissions(portal))
    again = engine.submit(db, s, prof, jid)
    assert again["outcome"] == "duplicate_blocked"
    assert len(submissions(portal)) == n
    assert db.one("SELECT COUNT(*) n FROM audit_log WHERE action='duplicate_submission_blocked'")["n"] == 1

    # the employer portal saying "already applied" is detected and not counted as a new verified submission
    db.conn.execute("UPDATE applications SET submitted_at=NULL, verified=0, retry_status=NULL WHERE job_id=?", (jid,))
    db.conn.execute("UPDATE jobs SET status='APPROVED' WHERE id=?", (jid,))
    db.conn.commit()
    out = engine.submit(db, s, prof, jid)
    assert out["status"] == "SUBMISSION_UNVERIFIED" and "existing application" in out["detail"]


# 11. Record a verified successful submission ----------------------------------------------------------------------------
def test_11_verified_submission_recorded(env, prepared):
    s, db, prof = prepared("1001")
    jid = job_id_by_employer(db, "Paymint")
    s.mode = "review"
    pipeline.approve(db, jid)
    pipeline.apply_pending(db, s, prof)
    app = db.application(jid)
    assert db.job(jid)["status"] == "SUBMITTED" and app["verified"] == 1 and app["reference"].startswith("TP-")
    ev = loads(app["verification"])["evidence"]
    assert Path(ev["screenshot"]).exists() and Path(ev["html"]).exists() and "/confirmation/" in ev["url"]
    rec = db.one("SELECT detail FROM audit_log WHERE action='submission_recorded' AND entity_id=?", (jid,))
    assert json.loads(rec["detail"])["verified"] is True


# 12. Mark uncertain outcomes as unverified --------------------------------------------------------------------------------
def test_12_uncertain_outcomes_unverified(env, prepared, portal):
    s, db, prof = prepared("1010")
    jid = job_id_by_employer(db, "Vagueco")
    s.mode = "review"
    pipeline.approve(db, jid)
    out = pipeline.apply_pending(db, s, prof)[0]
    assert out["status"] == "SUBMISSION_UNVERIFIED"
    assert db.application(jid)["verified"] == 0


def test_12b_interrupted_submission_is_not_resubmitted(env, prepared, portal):
    s, db, prof = prepared("1001")
    jid = job_id_by_employer(db, "Paymint")
    s.mode = "review"
    pipeline.approve(db, jid)
    app = db.application(jid)
    db.upsert_application(jid, app["idempotency_key"], retry_status="in_progress")  # simulated crash after click
    n = len(submissions(portal))
    out = engine.submit(db, s, prof, jid)
    assert out["status"] == "SUBMISSION_UNVERIFIED" and len(submissions(portal)) == n


# 13. Recover from ordinary navigation and form errors -----------------------------------------------------------------------
def test_13_recover_from_navigation_and_form_errors(env, prepared, portal):
    s, db, prof = prepared("1008,1009")
    s.mode = "review"
    httpx.post(portal.base_url + "/_admin/reset")  # fresh hit counters: first GET 503s, first POST "session expired"
    for emp in ("Flakyco", "Formerr"):
        pipeline.approve(db, job_id_by_employer(db, emp))
    results = {r["job_id"]: r for r in pipeline.apply_pending(db, s, prof)}
    for emp in ("Flakyco", "Formerr"):
        jid = job_id_by_employer(db, emp)
        assert results[jid]["outcome"] == "submitted", results[jid]
        assert db.application(jid)["attempts"] >= 2
    errors = db.q("SELECT detail FROM audit_log WHERE action='transient_error'")
    assert len(errors) >= 2


def test_13b_offsite_redirect_is_blocked_not_retried(env, prepared):
    s, db, prof = prepared("1012")
    jid = job_id_by_employer(db, "Redirecto")
    app = db.application(jid)
    assert "unapproved domain" in app["blocker"]
    s.mode = "review"
    pipeline.approve(db, jid)
    out = engine.submit(db, s, prof, jid)
    assert out["status"] == "FAILED" and "unapproved domain" in out["detail"]
    assert db.application(jid)["retry_status"] == "not_retryable"


# 14. Pause for human intervention when necessary ------------------------------------------------------------------------------
def test_14_pause_for_captcha(env, prepared, portal):
    s, db, prof = prepared("1007")
    jid = job_id_by_employer(db, "Captchaco")
    assert "CAPTCHA" in db.application(jid)["blocker"]
    s.mode = "review"
    pipeline.approve(db, jid)
    n = len(submissions(portal))
    out = engine.submit(db, s, prof, jid)
    assert out["outcome"] == "needs_human" and "CAPTCHA" in out["detail"]
    assert db.job(jid)["status"] == "READY_FOR_REVIEW" and len(submissions(portal)) == n


def test_14b_unexpected_question_then_candidate_answers(env, prepared, portal):
    s, db, prof = prepared("1003")
    jid = job_id_by_employer(db, "Ledgerly")
    q = "Describe your experience maintaining COBOL mainframe batch systems"
    with pytest.raises(ValueError, match="COBOL"):
        pipeline.approve(db, jid)
    pipeline.set_answer(db, jid, q, "I have not worked with COBOL mainframes.")
    pipeline.approve(db, jid)
    s.mode = "review"
    out = engine.submit(db, s, prof, jid)
    assert out["outcome"] == "submitted"
    assert submissions(portal)[-1]["fields"]["q_cobol"] == "I have not worked with COBOL mainframes."


# 15. Generate an exportable application report -------------------------------------------------------------------------------------
def test_15_export_report(env, prepared):
    s, db, prof = prepared("1001,1004,1006", extra_sources=True)
    s.mode = "review"
    pipeline.approve(db, job_id_by_employer(db, "Paymint"))
    pipeline.apply_pending(db, s, prof)
    rows = list(csv.DictReader(io.StringIO(report.export(db, "csv"))))
    assert len(rows) == db.one("SELECT COUNT(*) n FROM jobs")["n"]
    paymint = next(r for r in rows if r["employer"].startswith("Paymint"))
    assert paymint["status"] == "SUBMITTED" and paymint["submission_verified"] == "True" and paymint["reference"]
    assert paymint["baseline_score"] and paymint["final_score"] and paymint["resume_version"]
    stater = next(r for r in rows if r["employer"].startswith("Statesider"))
    assert stater["meets_criteria"] == "False" and "sponsorship" in stater["disqualifiers"]
    data = json.loads(report.export(db, "json"))
    assert data["summary"]["verified_submissions"] == 1
    assert "<table>" in report.export(db, "html")


# Authorization modes -------------------------------------------------------------------------------------------------------------------
def test_modes_and_consent(env, prepared, portal):
    s, db, prof = prepared("1001")
    jid = job_id_by_employer(db, "Paymint")
    n = len(submissions(portal))
    s.mode = "discovery"
    assert pipeline.apply_pending(db, s, prof) == []
    assert engine.submit(db, s, prof, jid)["outcome"] == "not_authorized"
    s.mode = "review"
    assert engine.submit(db, s, prof, jid)["outcome"] == "not_authorized"  # not approved yet
    s.mode = "auto"
    out = engine.submit(db, s, prof, jid)
    assert out["outcome"] == "not_authorized" and "consent" in out["detail"]
    with pytest.raises(ValueError):
        policy.grant_auto_consent(db, {})
    policy.grant_auto_consent(db, {"allowed_sources": ["greenhouse"], "allowed_domains": ["127.0.0.1"],
                                   "min_final_score": 99})
    out = engine.submit(db, s, prof, jid)
    assert out["outcome"] == "not_authorized" and "below consent minimum" in out["detail"]
    policy.revoke_auto_consent(db)
    policy.grant_auto_consent(db, {"allowed_sources": ["greenhouse"], "allowed_domains": ["127.0.0.1"],
                                   "min_final_score": 50})
    assert len(submissions(portal)) == n
    out = pipeline.apply_pending(db, s, prof)
    assert out[0]["outcome"] == "submitted"
    assert db.application(jid)["approved_by"] == "auto-apply policy"
