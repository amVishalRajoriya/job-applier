"""LinkedIn, Indeed and Naukri.com: discovery from alert emails and saved pages, assisted (human) apply.

Every page and email here is a SYNTHETIC fixture (tests/fixtures/jobsites/README.md). No test talks to
the real sites; the capture test serves fixtures through Playwright request interception.
"""
from __future__ import annotations

import email.message
import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from jobagent import cli, pipeline
from jobagent.answers import answer
from jobagent.apply import assisted, engine, policy
from jobagent.apply.adapters import FatalError, GenericFormAdapter
from jobagent.sources import jobsites as J
from jobagent.web.app import create_app

from .conftest import ROOT

FIX = ROOT / "tests" / "fixtures" / "jobsites"


def page(name: str):
    return J.parse_job_page(*J.read_saved_file(FIX / name))


# ---- URL recognition ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://www.linkedin.com/jobs/view/4012345678/", ("linkedin", "4012345678")),
    ("https://in.linkedin.com/jobs/view/backend-engineer-at-paymint-synthetic-4012345678?trk=x", ("linkedin", "4012345678")),
    ("https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=abc", ("linkedin", "4012345678")),
    ("https://www.linkedin.com/jobs/search/?currentJobId=4012345678&keywords=python", ("linkedin", "4012345678")),
    ("https://in.indeed.com/viewjob?jk=1A2B3C4D5E6F7A8B&from=serp", ("indeed", "1a2b3c4d5e6f7a8b")),
    ("https://www.indeed.com/rc/clk?jk=1a2b3c4d5e6f7a8b&fccid=x", ("indeed", "1a2b3c4d5e6f7a8b")),
    ("https://uk.indeed.com/jobs?q=python&vjk=0f0e0d0c0b0a0908", ("indeed", "0f0e0d0c0b0a0908")),
    ("https://www.naukri.com/job-listings-data-engineer-ledgerly-synthetic-pune-3-to-6-years-031025054321?src=x",
     ("naukri", "031025054321")),
    ("https://www.google.com/url?q=https%3A%2F%2Fin.indeed.com%2Frc%2Fclk%2Fdl%3Fjk%3D1a2b3c4d5e6f7a8b&sa=D",
     ("indeed", "1a2b3c4d5e6f7a8b")),
    ("https://eur01.safelinks.protection.outlook.com/?url=https%3A%2F%2Fwww.linkedin.com%2Fjobs%2Fview%2F4012345678%2F&data=x",
     ("linkedin", "4012345678")),
    ("https://www.linkedin.com/feed/", None),
    ("https://in.indeed.com/jobs?q=python", None),
    ("https://www.naukri.com/python-jobs-in-bengaluru", None),
    ("https://boards.greenhouse.io/acme/jobs/1", None),
    ("not a url", None),
])
def test_identify_job_urls(url, expected):
    ref = J.identify(url)
    assert (ref.site, ref.job_id) == expected if expected else ref is None


def test_canonical_urls_are_stable():
    a = J.identify("https://in.linkedin.com/jobs/view/backend-engineer-at-x-4012345678?trk=1")
    b = J.identify("https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=2")
    assert a.url == b.url == "https://www.linkedin.com/jobs/view/4012345678/"
    assert J.site_of("https://www.naukri.com/mnjuser/profile") == "naukri" and J.site_of("https://example.com") is None


# ---- saved pages -------------------------------------------------------------------------------

def test_linkedin_page_with_jsonld_and_external_ats_link():
    rec = page("linkedin_job_SYNTHETIC.html")
    assert (rec.source, rec.source_id, rec.title, rec.employer) == \
        ("linkedin", "linkedin:4012345678", "Backend Engineer", "Paymint (synthetic)")
    assert "Bengaluru" in rec.location and "PostgreSQL" in rec.description and rec.employment_type == "full-time"
    assert rec.original_url == "https://www.linkedin.com/jobs/view/4012345678/"
    # the "Apply" link goes to the employer's Greenhouse form; the unrelated greenhouse link in "similar jobs" is ignored
    assert rec.application_url == "https://job-boards.greenhouse.io/paymintsynthetic/jobs/7712345"
    assert rec.raw["untested_live"] and rec.raw["external_ats"] == rec.application_url


def test_indeed_page_without_jsonld_uses_page_structure():
    rec = page("indeed_job_SYNTHETIC.html")
    assert (rec.source_id, rec.title, rec.employer) == ("indeed:1a2b3c4d5e6f7a8b", "Python Developer", "Ledgerly (synthetic)")
    assert rec.location.startswith("Mumbai") and rec.remote_policy == "hybrid"
    assert (rec.salary_min, rec.salary_max, rec.salary_currency) == (1200000, 1800000, "INR")
    assert "3+ years of Python experience" in rec.description and "\n" in rec.description
    assert rec.application_url == "https://in.indeed.com/viewjob?jk=1a2b3c4d5e6f7a8b"


def test_naukri_pages_jsonld_and_fallback():
    rec = page("naukri_job_SYNTHETIC.html")   # URL comes from og:url, posting from the second JSON-LD block
    assert (rec.source_id, rec.title, rec.employer) == ("naukri:021025012345", "Senior Python Developer", "Paymint (synthetic)")
    assert (rec.salary_min, rec.salary_max, rec.salary_currency) == (3000000, 4200000, "INR")
    assert rec.experience_requirement == "5+ years"
    rec2 = page("naukri_nojsonld_SYNTHETIC.html")
    assert (rec2.title, rec2.employer, rec2.location) == ("Data Engineer", "Ledgerly (synthetic)", "Pune")
    assert rec2.experience_requirement == "3 - 6 years" and (rec2.salary_min, rec2.salary_max) == (800000, 1400000)
    assert "Key skills: Python" in rec2.description and "Airflow" in rec2.description


@pytest.mark.parametrize("name,site", [("linkedin_authwall_SYNTHETIC.html", "LinkedIn"),
                                       ("indeed_captcha_SYNTHETIC.html", "Indeed")])
def test_login_walls_and_captchas_are_reported_not_stored(name, site):
    with pytest.raises(J.PageBlocked, match=site):
        page(name)


def test_unrecognised_page_and_mhtml(tmp_path):
    with pytest.raises(ValueError, match="not a recognisable"):
        J.parse_job_page("<html><h1>Hello</h1></html>", "https://example.com/x")
    html = (FIX / "indeed_job_SYNTHETIC.html").read_text()
    msg = email.message.EmailMessage()
    msg["Snapshot-Content-Location"] = "https://in.indeed.com/viewjob?jk=1a2b3c4d5e6f7a8b"
    msg.set_content(html, subtype="html")
    (tmp_path / "job.mhtml").write_bytes(msg.as_bytes())
    got, url = J.read_saved_file(tmp_path / "job.mhtml")
    assert url.endswith("jk=1a2b3c4d5e6f7a8b") and J.parse_job_page(got, url).title == "Python Developer"


# ---- alert emails ------------------------------------------------------------------------------

def alert(name: str, **kw):
    return J.parse_alert_email((FIX / name).read_bytes(), **kw)


def test_linkedin_alert():
    site, jobs = alert("linkedin_alert_SYNTHETIC.eml")
    assert site == "linkedin"
    assert [(j.ref.job_id, j.title, j.company, j.location) for j in jobs] == [
        ("4012345678", "Backend Engineer", "Paymint (synthetic)", "Bengaluru, Karnataka, India"),
        ("4012345690", "Platform Engineer (Python)", "Shopwell (synthetic)", "India (Remote)")]


def test_indeed_alert_unwraps_redirects_and_keeps_snippet():
    site, jobs = alert("indeed_alert_SYNTHETIC.eml")
    assert site == "indeed" and [j.ref.job_id for j in jobs] == ["1a2b3c4d5e6f7a8b", "0f0e0d0c0b0a0908"]
    rec = J.alert_to_record(jobs[0], "2026-10-30")
    assert (rec.title, rec.employer, rec.location) == ("Python Developer", "Ledgerly (synthetic)", "Mumbai, Maharashtra")
    assert rec.salary_currency == "INR" and "Django" in rec.description and "only a short summary" in rec.description
    assert rec.raw["summary_only"] and rec.date_posted == "2026-10-30"
    assert J.alert_to_record(jobs[1], None).remote_policy == "remote"


def test_naukri_alert():
    site, jobs = alert("naukri_alert_SYNTHETIC.eml")
    assert site == "naukri" and len(jobs) == 2   # the duplicate "Apply" link did not create a third job
    rec = J.alert_to_record(jobs[0], None)
    assert (rec.title, rec.employer, rec.location) == ("Senior Python Developer", "Paymint (synthetic)", "Bengaluru")
    assert rec.experience_requirement == "5-8 Yrs" and rec.salary_min == 3000000
    assert "FastAPI" in rec.description


def test_spoofed_sender_is_ignored():
    assert alert("spoofed_alert_SYNTHETIC.eml") == (None, [])
    _, jobs = alert("spoofed_alert_SYNTHETIC.eml", require_known_sender=False)
    assert len(jobs) == 1   # only when the candidate turns the sender check off


def test_alert_drops_links_to_other_sites_when_strict():
    raw = (FIX / "linkedin_alert_SYNTHETIC.eml").read_text().replace(
        "https://www.linkedin.com/comm/jobs/view/4012345690/?trackingId=def",
        "https://www.naukri.com/job-listings-x-999999999999")
    _, jobs = J.parse_alert_email(raw)
    assert [j.ref.site for j in jobs] == ["linkedin"]


class FakeIMAP:
    calls: list = []

    def __init__(self, host, port):
        FakeIMAP.calls = [("connect", host, port)]

    def login(self, user, pw):
        FakeIMAP.calls.append(("login", user, pw))

    def select(self, mailbox, readonly=False):
        FakeIMAP.calls.append(("select", mailbox, readonly))
        return "OK", [b"3"]

    def search(self, charset, *criteria):
        FakeIMAP.calls.append(("search",) + criteria)
        hits = {"linkedin.com": b"1", "naukri.com": b"2"}
        return "OK", [hits.get(criteria[-1], b"")]

    def fetch(self, mid, what):
        FakeIMAP.calls.append(("fetch", mid, what))
        name = {b"1": "linkedin_alert_SYNTHETIC.eml", b"2": "naukri_alert_SYNTHETIC.eml"}[mid]
        return "OK", [(b"1 (BODY[] {1})", (FIX / name).read_bytes())]

    def logout(self):
        FakeIMAP.calls.append(("logout",))


def test_imap_is_read_only_and_password_comes_from_env(monkeypatch):
    cfg = {"imap": {"host": "imap.example.com", "user": "asha@example.com", "password_env": "TEST_IMAP_PW"},
           "_imap_class": FakeIMAP}
    with pytest.raises(RuntimeError, match="TEST_IMAP_PW"):
        list(J.JobAlertSource(None, **cfg).fetch())
    monkeypatch.setenv("TEST_IMAP_PW", "app-password")
    recs = list(J.JobAlertSource(None, **cfg).fetch())
    assert {r.source for r in recs} == {"linkedin", "naukri"} and len(recs) == 4
    assert ("select", "INBOX", True) in FakeIMAP.calls
    assert all(c[2] == "(BODY.PEEK[])" for c in FakeIMAP.calls if c[0] == "fetch")   # never marks mail as read
    assert not any(c[0] in ("store", "expunge", "copy", "move") for c in FakeIMAP.calls)


# ---- discovery through the pipeline -------------------------------------------------------------

def jobsite_dirs(tmp_path, pages=("linkedin_job_SYNTHETIC.html", "naukri_job_SYNTHETIC.html",
                                  "linkedin_authwall_SYNTHETIC.html")):
    alerts, saved = tmp_path / "alerts", tmp_path / "pages"
    alerts.mkdir()
    saved.mkdir()
    for n in ("linkedin_alert_SYNTHETIC.eml", "naukri_alert_SYNTHETIC.eml", "spoofed_alert_SYNTHETIC.eml"):
        shutil.copy(FIX / n, alerts / n)
    for n in pages:
        shutil.copy(FIX / n, saved / n)
    urls = tmp_path / "urls.txt"
    urls.write_text("https://www.linkedin.com/jobs/view/4012345678/\nhttps://in.indeed.com/viewjob?jk=1a2b3c4d5e6f7a8b\n")
    return [{"type": "job_alerts", "folder": str(alerts)},
            {"type": "saved_pages", "folder": str(saved), "urls_file": str(urls)}]


def test_discovery_merges_alerts_and_saved_pages(env, tmp_path):
    s, db, _ = env
    out = pipeline.run_discovery(db, s, jobsite_dirs(tmp_path))
    assert out["errors"] == []
    assert out["new_jobs"] == 4 and out["merged_duplicates"] == 2   # 4 alert jobs; 2 saved pages are the same jobs
    li = db.one("SELECT * FROM jobs WHERE title='Backend Engineer'")
    assert "Familiarity with Docker" in li["description"]            # the page's full description replaced the summary
    assert li["application_url"] == "https://job-boards.greenhouse.io/paymintsynthetic/jobs/7712345"
    srcs = {r["source"] for r in db.q("SELECT source FROM job_sources WHERE job_id=?", (li["id"],))}
    assert srcs == {"linkedin"}
    notes = " ".join(n["note"] for n in out["notes"])
    assert "linkedin_authwall_SYNTHETIC.html" in notes and "login wall" in notes
    assert "1 queued job URL(s) have no saved page yet" in notes
    pending = next(n for n in out["notes"] if "urls" in n)["urls"]
    assert pending == ["https://in.indeed.com/viewjob?jk=1a2b3c4d5e6f7a8b"]
    assert db.one("SELECT COUNT(*) n FROM jobs WHERE title LIKE 'Ignore previous%'")["n"] == 0


def test_direct_site_sources_are_refused(env):
    s, db, _ = env
    for kind in ("linkedin", "indeed", "naukri"):
        with pytest.raises(ValueError, match="job_alerts"):
            pipeline.run_discovery(db, s, [{"type": kind}])


def test_queue_urls(tmp_path):
    q = tmp_path / "q.txt"
    added, rejected = J.add_urls(q, ["https://in.linkedin.com/jobs/view/x-4012345678", "https://example.com/job",
                                     "https://www.linkedin.com/comm/jobs/view/4012345678/", "# note", ""])
    assert [r.job_id for r in added] == ["4012345678"] and rejected == ["https://example.com/job"]
    assert [r.url for r in J.queued_urls(q)] == ["https://www.linkedin.com/jobs/view/4012345678/"]


# ---- applying stays with the candidate ---------------------------------------------------------

def test_policy_never_automates_these_sites(env):
    s, db, _ = env
    for url in ("https://www.linkedin.com/jobs/view/1/", "https://uk.indeed.com/viewjob?jk=1a2b3c4d5e6f7a8b",
                "https://www.naukri.com/job-listings-x-021025012345"):
        key, status, _ = policy.capability_for(url)
        assert status == "assisted" and key in J.JOB_SITES
        assert policy.allowed_domains_for(url, ["linkedin.com", "indeed.com", "naukri.com", "example.org"]) \
            == ["example.org"]
    with pytest.raises(ValueError, match="never automated"):
        policy.grant_auto_consent(db, {"allowed_sources": ["linkedin"], "allowed_domains": ["linkedin.com"]})
    with pytest.raises(ValueError, match="never automated"):
        policy.grant_auto_consent(db, {"allowed_sources": ["greenhouse"], "allowed_domains": ["greenhouse.io"],
                                       "allowed_apply_status": ["assisted"]})


class _FakePage:
    url = "about:blank"

    def set_default_timeout(self, ms): ...
    def on(self, event, fn): ...
    def route(self, pattern, fn): ...

    def goto(self, url, **kw):
        raise AssertionError("the adapter must not navigate to these sites")


def test_form_adapter_refuses_job_sites_even_if_allowed():
    a = GenericFormAdapter(_FakePage(), ["linkedin.com", "www.naukri.com", "greenhouse.io"])
    assert a.allowed == ["greenhouse.io"]
    for url in ("https://www.linkedin.com/jobs/view/1/", "https://www.naukri.com/job-listings-x-021025012345"):
        with pytest.raises(FatalError, match="never automated"):
            a.open(url)


@pytest.fixture
def naukri_job(env, tmp_path):
    s, db, prof = env
    pipeline.run_discovery(db, s, jobsite_dirs(tmp_path, pages=("naukri_job_SYNTHETIC.html",))[1:])
    jid = db.one("SELECT id FROM jobs WHERE title='Senior Python Developer'")["id"]
    pipeline.screen_and_score(db, s, prof, [jid])
    return s, db, prof, jid


def test_naukri_job_is_prepared_then_paused_for_the_candidate(naukri_job):
    s, db, prof, jid = naukri_job
    job, app = db.job(jid), db.application(jid)
    assert job["status"] == "READY_FOR_REVIEW" and app["resume_version_id"]
    assert "never automated" in app["blocker"]
    pipeline.approve(db, jid)
    s.mode = "review"
    res = pipeline.apply_pending(db, s, prof, [jid])
    assert res[0]["outcome"] == "needs_human" and "Naukri" in res[0]["detail"]
    assert db.job(jid)["status"] == "READY_FOR_REVIEW" and not db.application(jid)["submitted_at"]
    rep = engine.dry_run(db, s, prof, jid)
    assert rep["outcome"] == "no_adapter" and rep["submitted"] is False


def test_application_kit_and_manual_submission(naukri_job):
    s, db, prof, jid = naukri_job
    k, path = assisted.write_kit(db, s.documents_dir, prof, jid)
    assert k["assisted"] and k["site"] == "naukri" and k["files"]
    by_q = {a["question"]: a for a in k["answers"]}
    assert by_q["Current CTC"]["status"] == "needs_input" and by_q["Current CTC"]["value"] is None
    assert by_q["Expected CTC"]["status"] == "answered" and by_q["Notice period"]["value"] == "60 days"
    text = path.read_text()
    assert "never automated" in text and "| Current CTC |  | needs you" in text
    r = assisted.record_manual_submission(db, jid, "NK-12345")
    assert r["outcome"] == "recorded"
    app = db.application(jid)
    assert db.job(jid)["status"] == "SUBMITTED" and app["verified"] == 0 and app["reference"] == "NK-12345"
    assert assisted.record_manual_submission(db, jid)["outcome"] == "already_recorded"
    assert engine.submit(db, s, prof, jid)["outcome"] == "duplicate_blocked"
    actions = [r["action"] for r in db.q("SELECT action FROM audit_log WHERE entity_id=?", (jid,))]
    assert "assist_kit_prepared" in actions and "manual_submission_recorded" in actions


def test_current_ctc_is_never_answered_with_expected_salary(profile):
    job = {"title": "x", "employer": "y", "location": "Bengaluru, India"}
    assert answer("Current CTC (in lakhs)", profile, job).status == "needs_input"
    profile.salary.current_text = "24 LPA"
    assert answer("What is your current salary?", profile, job).value == "24 LPA"
    assert answer("Expected CTC", profile, job).value != "24 LPA"


# ---- dashboard and CLI -------------------------------------------------------------------------

def _csrf(text):
    return text.split('name="csrf_token" value="')[1].split('"')[0]


def test_dashboard_setup_jobsites(env, tmp_path):
    s, db, _ = env
    (s.home / "config.yaml").write_text(cli.CONFIG_TEMPLATE)
    client = TestClient(create_app(s, db))
    page_ = client.get("/setup")
    assert "LinkedIn, Indeed and Naukri.com" in page_.text and "forbid scraping" in page_.text
    csrf = _csrf(page_.text)
    r = client.post("/setup/jobsites", data={"csrf_token": csrf, "imap_host": "imap.example.com",
                                             "imap_user": "a@example.com", "imap_password_env": "hunter2!"},
                    follow_redirects=False)
    assert "never+the+password" in r.headers["location"] or "never%20the%20password" in r.headers["location"]
    r = client.post("/setup/jobsites", data={
        "csrf_token": csrf, "pages_folder": str(tmp_path / "pages"), "alerts_folder": str(tmp_path / "alerts"),
        "imap_host": "imap.example.com", "imap_user": "a@example.com", "imap_password_env": "MY_IMAP_PW",
        "urls": "https://www.linkedin.com/jobs/view/4012345678/\nhttps://example.com/nope"}, follow_redirects=False)
    assert r.status_code == 303 and "queued+1" in r.headers["location"] and "example.com" in r.headers["location"]
    cfg = yaml.safe_load((s.home / "config.yaml").read_text())
    by_type = {c["type"]: c for c in cfg["sources"]}
    assert by_type["saved_pages"]["folder"] == str(tmp_path / "pages")
    assert by_type["job_alerts"]["imap"]["password_env"] == "MY_IMAP_PW" and "password" not in by_type["job_alerts"]["imap"]
    assert (tmp_path / "pages").is_dir() and (tmp_path / "alerts").is_dir()
    again = client.get("/setup").text
    assert "1 URL(s) queued, 1 still need a saved page" in again


def test_dashboard_job_page_for_assisted_job(naukri_job):
    s, db, prof, jid = naukri_job
    client = TestClient(create_app(s, db))
    html = client.get(f"/jobs/{jid}").text
    assert "Apply on Naukri.com yourself" in html and "Current CTC" in html
    assert "Dry run (never submits)" not in html and "Submit this application" not in html
    r = client.post(f"/jobs/{jid}/manual-submitted", data={"csrf_token": _csrf(html), "reference": "REF-9"},
                    follow_redirects=False)
    assert r.status_code == 303 and db.job(jid)["status"] == "SUBMITTED"
    assert "Recorded as SUBMITTED" in client.get(f"/jobs/{jid}").text


def test_cli_add_urls_and_assist(naukri_job, capsys):
    s, db, prof, jid = naukri_job
    home = str(s.home)
    cli.main(["--home", home, "add-urls", "https://in.indeed.com/viewjob?jk=1a2b3c4d5e6f7a8b", "https://example.com/x"])
    out = capsys.readouterr().out
    assert "queued 1 job URL(s)" in out and "skipped: https://example.com/x" in out
    cfg = yaml.safe_load((s.home / "config.yaml").read_text())
    assert any(c["type"] == "saved_pages" for c in cfg["sources"])
    cli.main(["--home", home, "assist", str(jid), "--no-open", "--no-ask"])
    out = capsys.readouterr().out
    assert "Apply yourself: Senior Python Developer" in out and f"assist {jid} --submitted" in out
    assert db.job(jid)["status"] == "READY_FOR_REVIEW"
    cli.main(["--home", home, "assist", str(jid), "--submitted", "--reference", "NK-1"])
    assert "recorded" in capsys.readouterr().out
    row = sqlite3.connect(s.db_path).execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row[0] == "SUBMITTED"


# ---- supervised capture (real browser, fixtures served by request interception) ----------------

def test_supervised_capture_saves_only_what_the_candidate_confirms(tmp_path):
    from jobagent.browser import launch_chromium
    from jobagent.capture import capture
    served = {"4012345678": "linkedin_job_SYNTHETIC.html", "4012345679": "linkedin_authwall_SYNTHETIC.html"}
    refs = [J.identify(f"https://www.linkedin.com/jobs/view/{i}/") for i in ("4012345678", "4012345679", "4012345680")]
    seen = []

    def serve(route, request):
        seen.append((request.method, request.url))
        name = next((f for k, f in served.items() if k in request.url), None)
        if name:
            route.fulfill(status=200, content_type="text/html", body=(FIX / name).read_text())
        else:
            route.fulfill(status=200, content_type="text/html", body="<html><body>other</body></html>")

    answers = iter(["", "", "s"])
    with launch_chromium(headless=True) as (_, browser):
        pg = browser.new_page()
        pg.route("**/*", serve)
        res = capture(pg, refs, tmp_path / "pages", lambda ref, i, n: next(answers), say=lambda m: None)
    assert [r["result"] for r in res] == ["saved", "blocked", "skipped"]
    assert {m for m, _ in seen} == {"GET"}                       # it only loads pages; nothing is posted
    files = list((tmp_path / "pages").iterdir())
    assert [f.name for f in files] == ["linkedin_4012345678.html"]
    recs = list(J.SavedPagesSource(None, folder=str(tmp_path / "pages")).fetch())
    assert recs[0].title == "Backend Engineer" and recs[0].source_id == "linkedin:4012345678"


def test_capture_stops_at_the_page_limit(tmp_path):
    from jobagent.capture import capture

    class P:
        url = "about:blank"
        loaded = []

        def goto(self, url, **kw):
            P.loaded.append(url)

        def content(self):
            return "<html></html>"

    refs = [J.identify(f"https://www.linkedin.com/jobs/view/40000000{i:02d}/") for i in range(5)]
    msgs = []
    res = capture(P(), refs, tmp_path, lambda *a: "s", say=msgs.append, max_pages=2)
    assert len(P.loaded) == 2 and len(res) == 2 and "at most 2" in msgs[0]
