"""SUPERSEDED: these probes now live, inverted, in tests/test_audit_regressions.py, and most of them fail
against the fixed code (that is expected). Kept as the record of how each bug was confirmed.

Audit probes from the 2026-11-06 audit (see AUDIT.md). Each test PASSES when the bug it describes is present.

Not part of the suite. To reproduce: cp docs/audit_2026-11-06/test_audit_probes.py tests/test_zz_audit_probes.py
then: python -m pytest -q tests/test_zz_audit_probes.py -s   (delete the copy afterwards).
After a fix, the matching probe should start failing; turn it into a real regression test with the assertion inverted.
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Error as PWError

from jobagent import pipeline
from jobagent.apply import engine
from jobagent.apply.adapters import GenericFormAdapter
from jobagent.filtering import screen
from jobagent.requirements import parse_job
from jobagent.sources import jobsites as J
from jobagent.web.app import create_app
from .conftest import job_id_by_employer  # noqa: F401

ALERT = """From: Naukri.com <info@naukri.com>
Subject: jobs
Date: Sat, 31 Oct 2026 05:15:00 +0530
MIME-Version: 1.0
Content-Type: text/html; charset=utf-8

<html><body><table><tr><td>
<a href="https://www.naukri.com/job-listings-senior-python-developer-paymint-synthetic-bengaluru-5-to-8-years-021025012345">Senior Python Developer</a>
<div>Paymint (synthetic)</div><div>Bengaluru</div>
<div>Python, Django, PostgreSQL. Own 2-3 large services and work with internal teams.</div>
</td></tr></table></body></html>
"""

def test_probe_alert_phantom_salary_and_internship(profile):
    site, jobs = J.parse_alert_email(ALERT)
    rec = J.alert_to_record(jobs[0], "2026-11-01")
    print("salary", rec.salary_min, rec.salary_max, rec.salary_currency, "type", rec.employment_type)
    d = rec.to_dict()
    res = screen(d, parse_job(d["description"]), profile)
    print("disqualifiers", res.disqualifiers)
    assert rec.employment_type == "internship"
    assert rec.salary_max == 300000.0
    assert not res.passed

def test_probe_exception_aborts_batch_and_strands_job(env, reset_portal, monkeypatch):
    from .conftest import sources
    s, db, prof = env
    pipeline.run_discovery(db, s, sources(reset_portal.base_url, "all")[:1])
    calls = {"n": 0}
    real = pipeline.template_cover_letter
    def boom(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("cover letter failed truth guard: simulated")
        return real(*a, **k)
    monkeypatch.setattr(pipeline, "template_cover_letter", boom)
    with pytest.raises(ValueError):
        pipeline.screen_and_score(db, s, prof)
    st = [r["status"] for r in db.q("SELECT status FROM jobs ORDER BY id")]
    print("after crash", st)
    out = pipeline.screen_and_score(db, s, prof)
    st2 = [r["status"] for r in db.q("SELECT status FROM jobs ORDER BY id")]
    print("after rerun", st2, out)
    assert any(x in ("SCORED", "OPTIMIZATION_REQUIRED", "OPTIMIZED") for x in st2)

def test_probe_tracking_invalid_transition_500(env, prepared):
    s, db, prof = prepared("1001")
    jid = job_id_by_employer(db, "Paymint")
    app = create_app(s, db)
    c = TestClient(app, raise_server_exceptions=False)
    import re
    html = c.get(f"/jobs/{jid}").text
    tok = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    r = c.post(f"/jobs/{jid}/tracking", data={"status": "INTERVIEW", "csrf_token": tok}, follow_redirects=False)
    print("status", db.job(jid)["status"], "->", r.status_code)
    assert r.status_code == 500

def test_probe_playwright_error_escapes_submit(env, prepared, monkeypatch):
    s, db, prof = prepared("1001")
    jid = job_id_by_employer(db, "Paymint")
    s.mode = "review"
    pipeline.approve(db, jid)
    def bad(self, *a, **k):
        raise PWError("Element is not attached to the DOM")
    monkeypatch.setattr(GenericFormAdapter, "submit", lambda self: (_ for _ in ()).throw(PWError("Target closed")))
    with pytest.raises(PWError):
        engine.submit(db, s, prof, jid)
    app = db.application(jid)
    print("after PW error:", db.job(jid)["status"], app["retry_status"])
    out = engine.submit(db, s, prof, jid)
    print("next run:", out)
    assert out["status"] == "SUBMISSION_UNVERIFIED"

def test_probe_naukri_queue_roundtrip(tmp_path):
    added, _ = J.add_urls(tmp_path / "q.txt", ["https://www.naukri.com/job-detail?jobId=123456789012"])
    assert added and J.queued_urls(tmp_path / "q.txt") == []

def test_probe_alert_then_saved_page_never_rescored(env):
    from jobagent.normalize import upsert_job
    s, db, prof = env
    from .conftest import ROOT
    FIX = ROOT / "tests" / "fixtures" / "jobsites"
    site, jobs = J.parse_alert_email((FIX / "naukri_alert_SYNTHETIC.eml").read_bytes())
    rec = J.alert_to_record(jobs[0], "2026-11-01")
    rec.employer = "Unknown employer"   # simulate an alert whose company line did not parse
    jid, _ = upsert_job(db, rec)
    pipeline.screen_and_score(db, s, prof)
    st1 = db.job(jid)["status"]; d1 = len(db.job(jid)["description"])
    page = J.parse_job_page(*J.read_saved_file(FIX / "naukri_job_SYNTHETIC.html"))
    jid2, created = upsert_job(db, page)
    pipeline.screen_and_score(db, s, prof)
    j = db.job(jid)
    print("same job", jid == jid2, "created", created, "status", st1, "->", j["status"], "desc", d1, "->", len(j["description"]), "employer", j["employer"], "page employer", page.employer)
    assert jid == jid2 and j["employer"] == "Unknown employer"

def test_probe_unknown_employer_alerts_merge(env):
    from jobagent.normalize import upsert_job
    s, db, prof = env
    a = J.AlertJob(J.JobRef("linkedin", "4000000001", "https://www.linkedin.com/jobs/view/4000000001/"), "Backend Engineer")
    b = J.AlertJob(J.JobRef("linkedin", "4000000002", "https://www.linkedin.com/jobs/view/4000000002/"), "Backend Engineer")
    i1, _ = upsert_job(db, J.alert_to_record(a, None)); i2, c2 = upsert_job(db, J.alert_to_record(b, None))
    print("two different LinkedIn jobs ->", i1, i2, "created second:", c2)
    assert i1 == i2


def test_probe_answers_inverted_and_wrong_years(profile):
    from jobagent.answers import answer
    job = {"location": "Bengaluru, India"}
    a = answer("Are you legally authorized to work in India without visa sponsorship?", profile, job, ["Yes", "No"])
    assert (a.value, a.status) == ("No", "answered")          # candidate is an Indian citizen: correct answer is Yes
    b = answer("How many years of experience do you have with Java?", profile, job, None)
    assert (b.value, b.status) == ("8", "answered")           # profile says Java: 1 year
    c = answer("Do you have experience building mobile apps?", profile, job, None)
    assert c.kind == "phone"


def test_probe_url_recognition_gaps():
    assert J.identify("https://cts.indeed.com/v3/abc?jk=0123456789abcdef").url.startswith("https://cts.indeed.com/")
    assert J.identify("https://www.linkedin.com/comm/jobs/view/3812345678/?url=https%3A%2F%2Fexample.com") is None


def test_probe_bad_charset_raises():
    raw = (b"From: LinkedIn <jobalerts-noreply@linkedin.com>\nSubject: x\nMIME-Version: 1.0\n"
           b"Content-Type: text/html; charset=unknown-8bit\n\n"
           b'<a href="https://www.linkedin.com/jobs/view/4012345678/">Backend Engineer</a>\n')
    with pytest.raises(LookupError):
        J.parse_alert_email(raw)


def test_probe_salary_parser_edges():
    from jobagent.sources.base import parse_salary
    assert parse_salary("Must speak 2 to 3 languages") == (200000.0, 300000.0, "INR")
    assert parse_salary("Salary: 12,00,000 - 18,00,000 INR") == (None, None, None)


def test_probe_reference_regex_false_positive():
    from jobagent.apply.adapters import REFERENCE
    assert REFERENCE.search("Thank you for your application. Reference checks happen later.").group(1) == "checks"
