"""Regression tests for the bugs found in the 2026-11-06 audit (AUDIT.md).

Each test started life as a probe in docs/audit_2026-11-06/test_audit_probes.py that passed while the bug was
present; here the assertion is inverted (and widened), so it fails on the old code and passes on the fix.
Issue ids (H1, M5, ...) refer to AUDIT.md. All data is SYNTHETIC.
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Error as PWError

from jobagent import pipeline
from jobagent.answers import answer, classify
from jobagent.apply import engine
from jobagent.apply.adapters import REFERENCE, GenericFormAdapter
from jobagent.filtering import screen
from jobagent.normalize import upsert_job
from jobagent.requirements import parse_job
from jobagent.sources import jobsites as J
from jobagent.sources.base import JobRecord, normalize_employment_type, parse_salary
from jobagent.web.app import create_app

from .conftest import ROOT, job_id_by_employer, sources

FIX = ROOT / "tests" / "fixtures" / "jobsites"
IN_JOB = {"location": "Bengaluru, India"}

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


# ---- H1: work authorization with "without sponsorship" ------------------------------------------------

@pytest.mark.parametrize("q,expected", [
    ("Are you legally authorized to work in India without visa sponsorship?", "Yes"),
    ("Can you work in India without requiring visa sponsorship?", "Yes"),
    ("Are you authorized to work in India without the need for employer sponsorship?", "Yes"),
    ("Are you legally authorized to work in the United States without sponsorship?", "No"),
    ("Are you eligible to work in the United Kingdom without requiring a visa?", "No"),
])
def test_h1_authorized_without_sponsorship_polarity(profile, q, expected):
    a = answer(q, profile, IN_JOB, ["Yes", "No"])
    assert (a.value, a.status) == (expected, "answered"), a


def test_h1_plain_questions_unchanged_and_mixed_ones_need_the_candidate(profile):
    assert answer("Are you legally authorized to work in India?", profile, IN_JOB, ["Yes", "No"]).value == "Yes"
    assert answer("Will you now or in the future require visa sponsorship to work in India?", profile, IN_JOB,
                  ["Yes", "No"]).value == "No"
    assert answer("Will you require sponsorship to work in the United States?", profile, IN_JOB,
                  ["Yes", "No"]).value == "Yes"
    # both topics, no clear polarity, or no "need": never guessed
    assert answer("Are you authorized to work in India, or will you need visa sponsorship?", profile,
                  IN_JOB).status == "needs_input"
    assert answer("Do you hold a valid visa for India?", profile, IN_JOB).status == "needs_input"
    assert answer("Do you require a work permit for India?", profile, IN_JOB).status == "needs_input"


# ---- H2: per-skill years -------------------------------------------------------------------------------

def test_h2_skill_years_use_the_skill_not_total_years(profile):
    b = answer("How many years of experience do you have with Java?", profile, IN_JOB, None)
    assert (b.value, b.status, b.source) == ("1", "answered", "skills.Java.years")
    assert answer("How many years of Python experience do you have?", profile, IN_JOB).value == "6"
    assert answer("How many years of experience do you have with Kubernetes?", profile, IN_JOB).status == "needs_input"
    assert answer("How many years of experience do you have with Java and Python?", profile,
                  IN_JOB).status == "needs_input"
    assert answer("How many years of professional experience do you have?", profile, IN_JOB).value == "8"
    assert answer("How many years of experience do you have in total?", profile, IN_JOB).value == "8"


# ---- L1: over-broad question rules -------------------------------------------------------------------

@pytest.mark.parametrize("q,kind", [
    ("Do you have experience building mobile apps?", "unknown"),
    ("Are you open to joining an early-stage startup?", "unknown"),
    ("Have you worked on payroll or compensation systems?", "unknown"),
    ("Do you have a degree in Computer Science?", "unknown"),
    ("Phone", "phone"), ("Mobile Number", "phone"), ("Phone number", "phone"),
    ("Expected compensation", "salary"), ("What are your salary expectations?", "salary"),
    ("What is your highest level of education?", "education"), ("Date of joining", "notice_period"),
])
def test_l1_question_rules_are_anchored(q, kind):
    assert classify(q) == kind


def test_l1_yes_no_select_never_gets_a_non_yes_no_value(profile):
    a = answer("Mobile number verified?", profile, IN_JOB, ["Yes", "No"])
    assert a.value is None and a.status == "needs_input"


# ---- H3, H4, L7, M7: salary and employment type -------------------------------------------------------

@pytest.mark.parametrize("text", ["Must speak 2 to 3 languages", "Python 2-3 libraries",
                                  "Team of 5 - 10 Engineers in 3 to 4 locations", "Own 2-3 large services"])
def test_h3_no_phantom_salary(text):
    assert parse_salary(text) == (None, None, None)


@pytest.mark.parametrize("text,expected", [
    ("₹ 12-18 Lacs P.A.", (1200000.0, 1800000.0, "INR")), ("15 - 25 LPA", (1500000.0, 2500000.0, "INR")),
    ("12-18 lakhs", (1200000.0, 1800000.0, "INR")), ("$120k - $150k", (120000.0, 150000.0, "USD")),
    ("Salary: 12,00,000 - 18,00,000 INR", (1200000.0, 1800000.0, "INR")),   # L7
])
def test_h3_real_salaries_still_parse(text, expected):
    assert parse_salary(text) == expected


@pytest.mark.parametrize("text,kind", [("Work with internal teams", None), ("An international payments company", None),
                                       ("Internship", "internship"), ("Summer intern, 3 months", "internship")])
def test_h4_intern_needs_a_whole_word(text, kind):
    assert normalize_employment_type(text) == kind


def test_h3_h4_alert_is_not_hard_rejected(profile):
    site, jobs = J.parse_alert_email(ALERT)
    rec = J.alert_to_record(jobs[0], "2026-11-01")
    assert rec.employment_type is None and rec.salary_max is None
    d = rec.to_dict()
    res = screen(d, parse_job(d["description"]), profile)
    assert not any("salary" in x or "employment type" in x for x in res.disqualifiers), res.disqualifiers


def test_h3_h4_free_text_guesses_only_warn(profile):
    base = JobRecord("naukri", "naukri:1", "Senior Python Developer", "Paymint (synthetic)",
                     description="Python, Django, PostgreSQL backend services.", location="Bengaluru",
                     salary_max=300000.0, salary_currency="INR", employment_type="internship")
    structured = screen(base.to_dict(), parse_job(base.description), profile)
    assert any("salary max" in x for x in structured.disqualifiers)
    assert any("employment type" in x for x in structured.disqualifiers)
    base.raw = {"salary_from_text": True, "employment_type_from_text": True}
    guessed = screen(base.to_dict(), parse_job(base.description), profile)
    assert not any("salary" in x or "employment type" in x for x in guessed.disqualifiers)
    assert any("salary max" in w for w in guessed.warnings) and any("employment type" in w for w in guessed.warnings)


def test_m7_salary_interval_is_normalized(profile):
    def res(smax, interval):
        r = JobRecord("lever", "x:1", "Senior Python Developer", "Paymint (synthetic)", description="Python backend.",
                      location="Bengaluru", salary_max=smax, salary_currency="INR", salary_interval=interval)
        return screen(r.to_dict(), parse_job(r.description), profile)
    assert not any("salary" in x for x in res(400000.0, "per-month").disqualifiers)   # 48 L a year
    assert any("salary" in x for x in res(100000.0, "month").disqualifiers)           # 12 L a year
    assert not any("salary" in x for x in res(100.0, "fortnight").disqualifiers)      # unknown: warn only
    assert any("salary" in x for x in res(1000000.0, "year").disqualifiers)


# ---- H5: one bad job does not stop the batch or strand itself --------------------------------------------

def test_h5_error_does_not_abort_batch_and_job_is_retried(env, reset_portal, monkeypatch):
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
    out = pipeline.screen_and_score(db, s, prof)          # does not raise
    assert len(out["errors"]) == 1 and "simulated" in out["errors"][0]["error"]
    failed = out["errors"][0]["job"]
    st = {r["id"]: r["status"] for r in db.q("SELECT id, status FROM jobs")}
    assert st[failed] == "PREPARATION_FAILED"
    assert "DISCOVERED" not in st.values()                 # the rest of the batch was processed
    assert db.q("SELECT 1 FROM audit_log WHERE action='preparation_error'")
    out2 = pipeline.screen_and_score(db, s, prof)          # next run retries it
    assert db.job(failed)["status"] == "READY_FOR_REVIEW" and not out2["errors"]
    assert pipeline.screen_and_score(db, s, prof)["screened"] == 0   # nothing left to retry


def test_h5_jobs_stranded_by_an_older_crash_are_picked_up(env, reset_portal):
    s, db, prof = env
    pipeline.run_discovery(db, s, sources(reset_portal.base_url, "all")[:1])
    jid = job_id_by_employer(db, "Paymint")
    db.set_status(jid, "SCREENED", "test")
    db.update_job(jid, screen_result={"passed": True})
    db.set_status(jid, "SCORED", "test")                    # what the old code left behind
    pipeline.screen_and_score(db, s, prof)
    assert db.job(jid)["status"] == "READY_FOR_REVIEW"


# ---- M12: dashboard tracking with a disallowed transition ----------------------------------------------

def test_m12_tracking_invalid_transition_redirects_with_message(env, prepared):
    s, db, prof = prepared("1001")
    jid = job_id_by_employer(db, "Paymint")
    c = TestClient(create_app(s, db), raise_server_exceptions=False)
    tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get(f"/jobs/{jid}").text).group(1)
    r = c.post(f"/jobs/{jid}/tracking", data={"status": "INTERVIEW", "csrf_token": tok}, follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]
    assert db.job(jid)["status"] == "READY_FOR_REVIEW"
    assert "submitted first" in c.get(r.headers["location"]).text
    assert c.post("/jobs/999999/tracking", data={"status": "CLOSED", "csrf_token": tok},
                  follow_redirects=False).status_code == 404


# ---- M1: Playwright errors -----------------------------------------------------------------------------

def test_m1_browser_error_after_click_is_recorded_unverified(env, prepared, monkeypatch):
    s, db, prof = prepared("1001")
    jid = job_id_by_employer(db, "Paymint")
    s.mode = "review"
    pipeline.approve(db, jid)
    monkeypatch.setattr(GenericFormAdapter, "submit", lambda self: (_ for _ in ()).throw(PWError("Target closed")))
    out = engine.submit(db, s, prof, jid)                   # does not raise
    assert out["status"] == "SUBMISSION_UNVERIFIED" and "Target closed" in out["detail"]
    assert db.application(jid)["verified"] == 0


def test_m1_browser_error_before_click_is_retried_then_failed(env, prepared, monkeypatch):
    s, db, prof = prepared("1001")
    jid = job_id_by_employer(db, "Paymint")
    s.mode = "review"
    s.max_apply_attempts = 2
    pipeline.approve(db, jid)
    monkeypatch.setattr(GenericFormAdapter, "fields",
                        lambda self: (_ for _ in ()).throw(PWError("Element is not attached to the DOM")))
    out = engine.submit(db, s, prof, jid)
    assert out["status"] == "FAILED" and "not attached" in out["detail"]
    assert db.application(jid)["submitted_at"] is None


# ---- M2, M3: how the result of a submission is recorded -------------------------------------------------

def _approve_variant(prepared, variant):
    s, db, prof = prepared("1001")
    jid = job_id_by_employer(db, "Paymint")
    db.update_job(jid, application_url=db.job(jid)["application_url"] + f"?variant={variant}")
    s.mode = "review"
    pipeline.approve(db, jid)
    return s, db, prof, jid


def test_m2_form_submitted_by_fetch_is_verified(env, prepared, portal):
    s, db, prof, jid = _approve_variant(prepared, "fetch")
    out = engine.submit(db, s, prof, jid)
    assert out["status"] == "SUBMITTED" and out["reference"].startswith("TP-"), out


def test_m3_redirect_offsite_after_submit_is_unverified_not_failed(env, prepared, portal):
    s, db, prof, jid = _approve_variant(prepared, "thanks_offsite")
    out = engine.submit(db, s, prof, jid)
    assert out["status"] == "SUBMISSION_UNVERIFIED", out
    assert "employer-thanks.invalid" in out["detail"]
    assert db.application(jid)["submitted_at"] is not None   # it can't be re-approved and sent twice


# ---- M4: reference numbers -----------------------------------------------------------------------------

@pytest.mark.parametrize("text,ref", [
    ("Thank you for your application. Reference checks happen later.", None),
    ("Thank you for your application. Your reference is pending.", None),
    ("Application received. Reference: TP-1001", "TP-1001"),
    ("Your confirmation number is ABC12345.", "ABC12345"),
    ("Application ID #98765", "98765"),
])
def test_m4_reference_needs_a_real_code(text, ref):
    m = REFERENCE.search(text)
    assert (m.group(1) if m else None) == ref


# ---- M5, M6: alert jobs in storage ---------------------------------------------------------------------

def test_m5_unknown_employer_alerts_do_not_merge(env):
    s, db, prof = env
    a = J.AlertJob(J.JobRef("linkedin", "4000000001", "https://www.linkedin.com/jobs/view/4000000001/"), "Backend Engineer")
    b = J.AlertJob(J.JobRef("linkedin", "4000000002", "https://www.linkedin.com/jobs/view/4000000002/"), "Backend Engineer")
    i1, _ = upsert_job(db, J.alert_to_record(a, None))
    i2, c2 = upsert_job(db, J.alert_to_record(b, None))
    assert i1 != i2 and c2
    i3, c3 = upsert_job(db, J.alert_to_record(a, None))      # the same job again still merges
    assert i3 == i1 and not c3


def test_m6_saved_page_after_alert_replaces_summary_and_rescores(env):
    s, db, prof = env
    site, jobs = J.parse_alert_email((FIX / "naukri_alert_SYNTHETIC.eml").read_bytes())
    rec = J.alert_to_record(jobs[0], "2026-11-01")
    rec.employer = "Unknown employer"   # an alert whose company line did not parse
    jid, _ = upsert_job(db, rec)
    pipeline.screen_and_score(db, s, prof)
    d1 = len(db.job(jid)["description"])
    page = J.parse_job_page(*J.read_saved_file(FIX / "naukri_job_SYNTHETIC.html"))
    jid2, created = upsert_job(db, page)
    assert jid2 == jid and not created
    j = db.job(jid)
    assert j["employer"] == page.employer and j["status"] == "DISCOVERED"
    pipeline.screen_and_score(db, s, prof)
    j = db.job(jid)
    assert j["status"] == "READY_FOR_REVIEW" and len(j["description"]) != d1
    assert db.q("SELECT 1 FROM audit_log WHERE action='summary_replaced_by_full_page'")


def test_m6_approved_job_is_not_reset(env):
    s, db, prof = env
    site, jobs = J.parse_alert_email((FIX / "naukri_alert_SYNTHETIC.eml").read_bytes())
    jid, _ = upsert_job(db, J.alert_to_record(jobs[0], "2026-11-01"))
    pipeline.screen_and_score(db, s, prof)
    app = db.application(jid)
    db.upsert_application(jid, app["idempotency_key"], approved_at="2026-11-02T00:00:00+00:00")
    before = db.job(jid)["status"]
    upsert_job(db, J.parse_job_page(*J.read_saved_file(FIX / "naukri_job_SYNTHETIC.html")))
    assert db.job(jid)["status"] == before


# ---- M8: unknown charsets ------------------------------------------------------------------------------

BAD_CHARSET = (b"From: LinkedIn <jobalerts-noreply@linkedin.com>\nSubject: x\nMIME-Version: 1.0\n"
               b"Content-Type: text/html; charset=unknown-8bit\n\n"
               b'<a href="https://www.linkedin.com/jobs/view/4012345678/">Backend Engineer</a>\n')


def test_m8_bad_charset_is_decoded_not_raised():
    site, jobs = J.parse_alert_email(BAD_CHARSET)
    assert [j.ref.job_id for j in jobs] == ["4012345678"]


def test_m8_one_bad_message_does_not_lose_the_folder(tmp_path, monkeypatch):
    for n in ("linkedin_alert_SYNTHETIC.eml", "naukri_alert_SYNTHETIC.eml"):
        (tmp_path / n).write_bytes((FIX / n).read_bytes())
    (tmp_path / "0_bad.eml").write_bytes(BAD_CHARSET)
    src = J.JobAlertSource(None, folder=str(tmp_path))
    real = J.parse_alert_email

    def flaky(msg, **k):
        if msg.get("Subject") == "x":
            raise RuntimeError("simulated parser failure")
        return real(msg, **k)
    monkeypatch.setattr(J, "parse_alert_email", flaky)
    recs = list(src.fetch())
    assert {r.source for r in recs} == {"linkedin", "naukri"}
    assert src.problems and "simulated" in src.problems[0]


# ---- M9, M10, M11, L8: job URLs ------------------------------------------------------------------------

def test_m9_naukri_queue_roundtrip(tmp_path):
    added, _ = J.add_urls(tmp_path / "q.txt", ["https://www.naukri.com/job-detail?jobId=123456789012"])
    assert added and [r.source_id for r in J.queued_urls(tmp_path / "q.txt")] == ["naukri:123456789012"]


@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/jobs/view/3812345678/",
    "https://www.linkedin.com/jobs/search/?currentJobId=3812345678",
    "https://in.indeed.com/viewjob?jk=0123456789abcdef",
    "https://cts.indeed.com/v3/abc?jk=0123456789abcdef",
    "https://www.naukri.com/job-listings-data-engineer-ledgerly-synthetic-pune-3-to-6-years-031025054321?src=x",
    "https://www.naukri.com/job-detail?jobId=123456789012",
    "https://www.naukrigulf.com/python-developer-jobs-in-dubai-uae-n-cd-10000-jid-020925500123",
])
def test_m9_identify_roundtrips(url):
    ref = J.identify(url)
    assert ref is not None and J.identify(ref.url) == ref


def test_m10_indeed_tracking_host_maps_to_public_host():
    assert J.identify("https://cts.indeed.com/v3/abc?jk=0123456789abcdef").url == \
        "https://www.indeed.com/viewjob?jk=0123456789abcdef"
    assert J.identify("https://in.indeed.com/viewjob?jk=0123456789abcdef").url.startswith("https://in.indeed.com/")


def test_m11_job_link_with_url_param_is_kept():
    ref = J.identify("https://www.linkedin.com/comm/jobs/view/3812345678/?url=https%3A%2F%2Fexample.com")
    assert ref is not None and ref.job_id == "3812345678"
    # real wrappers are still unwrapped
    assert J.identify("https://www.google.com/url?q=https%3A%2F%2Fwww.linkedin.com%2Fjobs%2Fview%2F3812345678%2F") == ref


def test_l8_naukrigulf_is_recognised():
    ref = J.identify("https://www.naukrigulf.com/python-developer-jobs-in-dubai-uae-n-cd-10000-jid-020925500123?x=1")
    assert ref.site == "naukri" and ref.job_id == "gulf-020925500123"


# ---- L5: excluded employers match longer legal names ---------------------------------------------------

def test_l5_excluded_employer_matches_legal_name(profile):
    from jobagent.normalize import employer_matches
    assert employer_matches("PayU Payments Pvt Ltd", ["PayU"])
    assert employer_matches("payu", ["PayU Payments Private Limited"])
    assert not employer_matches("PayUmoney Labs", ["PayU"]) and not employer_matches("Paymint", ["PayU"])
    prof = profile.model_copy(deep=True)
    prof.preferences.excluded_employers = ["Paymint"]
    r = JobRecord("lever", "x:1", "Senior Python Developer", "Paymint (synthetic) Pvt Ltd",
                  description="Python backend.", location="Bengaluru")
    assert "employer is on the candidate's exclusion list" in screen(r.to_dict(), parse_job(r.description), prof).disqualifiers
