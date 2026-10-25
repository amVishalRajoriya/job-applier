"""First-run setup, supervised dry run and the tightened limitations. SYNTHETIC DATA ONLY."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from jobagent import cli, onboarding, pipeline
from jobagent import profile as P
from jobagent.apply import engine
from jobagent.apply.adapters import GenericFormAdapter
from jobagent.apply.policy import allowed_domains_for
from jobagent.config import load_settings
from jobagent.db import Database, loads
from jobagent.documents import write_version
from jobagent.resume_doc import master_resume
from jobagent.resume_parser import parse_resume
from jobagent.skills import extract_skills, register_custom_skills
from jobagent.web.app import create_app

from .conftest import ROOT, job_id_by_employer


def submissions(portal):
    return httpx.get(portal.base_url + "/_admin/submissions").json()


# ---- supervised dry run ----------------------------------------------------------------------------

def test_dry_run_fills_real_form_but_never_submits(env, prepared, portal):
    s, db, prof = prepared("1001")
    jid = job_id_by_employer(db, "Paymint")
    before = db.job(jid)["status"]
    rep = engine.dry_run(db, s, prof, jid)
    assert rep["outcome"] == "ready_to_submit", rep
    assert rep["submitted"] is False
    assert submissions(portal) == []
    assert db.job(jid)["status"] == before  # a dry run never changes the job's status
    actions = {f["field"]: f["action"] for f in rep["fields"]}
    assert actions["Email"] == "filled" and actions["Resume/CV"] == "uploaded"
    assert Path(rep["evidence"]["screenshot"]).exists()
    assert "Nothing was submitted" in Path(rep["report"]).read_text()
    assert db.one("SELECT 1 FROM audit_log WHERE action='dry_run' AND entity_id=?", (jid,))


def test_dry_run_blocks_background_requests(env, prepared, portal):
    s, db, prof = prepared("1001")
    jid = job_id_by_employer(db, "Paymint")
    db.update_job(jid, application_url=db.job(jid)["application_url"] + "?variant=autosave")
    rep = engine.dry_run(db, s, prof, jid)
    assert rep["blocked_requests"], "the page's autosave POST should have been intercepted"
    assert httpx.get(portal.base_url + "/_admin/hits").json().get("beacon", 0) == 0
    assert submissions(portal) == []


def test_read_only_adapter_refuses_to_submit(env, prepared, portal):
    s, db, prof = prepared("1001")
    job = db.job(job_id_by_employer(db, "Paymint"))
    with engine.isolated_page(s) as page:
        a = GenericFormAdapter(page, allowed_domains_for(job["application_url"], []), 8000, read_only=True)
        a.open(job["application_url"])
        with pytest.raises(RuntimeError):
            a.submit()
        page.click("#submit_app")  # even a direct click cannot send the form
        page.wait_for_timeout(300)
    assert submissions(portal) == []


def test_dry_run_reports_open_questions(env, prepared, portal):
    s, db, prof = prepared("1003")
    jid = job_id_by_employer(db, "Ledgerly")
    rep = engine.dry_run(db, s, prof, jid)
    assert rep["outcome"] == "needs_input"
    assert rep["open_questions"]
    assert submissions(portal) == []


def test_multi_step_form_pauses_with_reason(env, prepared, portal):
    s, db, prof = prepared("1001")
    jid = job_id_by_employer(db, "Paymint")
    s.mode = "review"
    pipeline.approve(db, jid)
    db.update_job(jid, application_url=db.job(jid)["application_url"] + "?variant=wizard")
    out = pipeline.apply_pending(db, s, prof)
    assert out[0]["outcome"] == "needs_human" and "multi-step" in out[0]["detail"]
    assert submissions(portal) == []


# ---- first-run setup -------------------------------------------------------------------------------

def _details(tmp_path) -> Path:
    d = yaml.safe_load(onboarding.details_template())
    d["contact"]["phone"] = "+91 90000 00002"
    d["work_authorization"]["authorized"] = {"IN": "citizen"}
    d["work_authorization"]["requires_sponsorship"] = {"IN": "no", "US": "yes"}
    d["salary"].update(currency="INR", answer_text="Negotiable")
    d["notice_period_days"] = 30
    d["preferences"].update(target_roles=["Backend Engineer"], locations=["Bengaluru"], willing_to_relocate=False,
                            remote=["remote", "hybrid"])
    d["skills"] = {"Python": "advanced", "Temporalwidget": "beginner"}
    d["approved_answers"] = {"How did you hear about us?": "Careers page"}
    d["boards"] = ["https://boards.greenhouse.io/acmecorp", "Shop Well | https://jobs.lever.co/shopwell",
                   "https://www.linkedin.com/jobs/view/1", "https://acme.wd5.myworkdayjobs.com/en-US/Careers"]
    d["mode"] = "review"
    path = tmp_path / "details.yaml"
    path.write_text(yaml.safe_dump(d))
    return path


def test_setup_from_resume_and_details_file(tmp_path, monkeypatch, capsys):
    from jobagent.security import generate_key
    monkeypatch.setenv("JOBAGENT_ENCRYPTION_KEY", generate_key())
    home = tmp_path / "home"
    resume = ROOT / "data" / "sample" / "master_resume_SYNTHETIC.md"
    cli.main(["--home", str(home), "setup", "--resume", str(resume), "--details", str(_details(tmp_path))])
    out = capsys.readouterr().out
    assert "profile saved (encrypted)" in out and "linkedin" in out.lower()
    prof = P.load(Database(home / "jobagent.db"))
    assert prof.work_authorization.requires_sponsorship == {"IN": False, "US": True}
    assert prof.notice_period_days == 30 and prof.preferences.remote == ["remote", "hybrid"]
    assert prof.skill_entry("Python").proficiency == "advanced"
    added = prof.skill_entry("Temporalwidget")
    assert added and added.on_master_resume is False  # declared, but kept off the master resume
    assert prof.missing_personal_fields() == []
    cfg = yaml.safe_load((home / "config.yaml").read_text())
    assert cfg["mode"] == "review"
    assert {c["type"] for c in cfg["sources"]} == {"greenhouse", "lever", "workday"}
    assert {"type": "lever", "site": "shopwell", "employer": "Shop Well"} in cfg["sources"]
    assert next(c for c in cfg["sources"] if c["type"] == "workday")["site"] == "Careers"
    assert "Temporalwidget" in cfg["custom_skills"]
    # running setup again with a new resume keeps the personal answers
    cli.main(["--home", str(home), "setup", "--resume", str(resume), "--non-interactive"])
    assert P.load(Database(home / "jobagent.db")).notice_period_days == 30


def test_setup_never_enables_auto_mode(tmp_path):
    cfg = tmp_path / "config.yaml"
    with pytest.raises(ValueError):
        onboarding.update_config(cfg, mode="auto")


def test_setup_blank_answers_are_not_guessed(profile):
    blank = profile.model_copy(update={"notice_period_days": None})
    out = onboarding.apply_details(blank, yaml.safe_load(onboarding.details_template()))
    assert out.notice_period_days is None
    assert "notice_period_days" in onboarding.unanswered(out)


def test_interactive_interview(profile):
    answers = iter(["", "", "", "", "", "", "IN=citizen", "IN=maybe", "IN=no", "", "", "", "", "45"] + [""] * 50)
    details, boards = onboarding.interview(profile, ask=lambda _: next(answers), say=lambda _: None)
    assert details["work_authorization"]["requires_sponsorship"] == "IN=no"  # invalid answer was asked again
    assert details["notice_period_days"] == "45" and boards == []


def test_boards_from_urls():
    srcs, problems = onboarding.boards_from_entries([
        "https://job-boards.greenhouse.io/stripe", "https://jobs.ashbyhq.com/openco/123",
        "https://example.com/careers", "https://example.com/jobs.rss", "https://in.indeed.com/jobs?q=x",
        {"url": "jobs.lever.co/acme", "employer": "Acme Inc"}])
    assert [s["type"] for s in srcs] == ["greenhouse", "ashby", "careerpage", "feed", "lever"]
    assert srcs[0]["board_token"] == "stripe" and srcs[1]["organization"] == "openco"
    assert srcs[4] == {"type": "lever", "site": "acme", "employer": "Acme Inc"}
    assert len(problems) == 1 and "Indeed" in problems[0]
    for cfg in srcs:
        again, _ = onboarding.boards_from_entries(onboarding.source_to_entry(cfg))
        assert again == [cfg]


def test_dashboard_setup_page(env, sample_files):
    s, db, prof = env
    (s.home / "config.yaml").write_text(cli.CONFIG_TEMPLATE)
    client = TestClient(create_app(s, db))
    page = client.get("/setup")
    assert page.status_code == 200 and "Your details" in page.text
    csrf = page.text.split('name="csrf_token" value="')[1].split('"')[0]
    form = {"csrf_token": csrf, "notice_period_days": "90", "work_authorization.requires_sponsorship": "IN=no, GB=yes",
            "boards": "https://boards.greenhouse.io/acmecorp\nhttps://www.linkedin.com/jobs", "mode": "review",
            "skill:Python": "expert"}
    r = client.post("/setup/details", data=form, follow_redirects=False)
    assert r.status_code == 303 and "skipped" in r.headers["location"]
    p2 = P.load(db)
    assert p2.notice_period_days == 90 and p2.work_authorization.requires_sponsorship["GB"] is True
    assert p2.skill_entry("Python").proficiency == "expert"
    cfg = yaml.safe_load((s.home / "config.yaml").read_text())
    assert cfg["sources"] == [{"type": "greenhouse", "board_token": "acmecorp", "employer": "Acmecorp"}]
    assert client.post("/setup/details", data={**form, "csrf_token": "bad"}).status_code == 403
    # resume upload keeps the answers just saved
    with open(sample_files["docx"], "rb") as f:
        r = client.post("/setup/resume", data={"csrf_token": csrf}, files={"file": ("cv.docx", f.read())},
                        follow_redirects=False)
    assert r.status_code == 303 and "resume+read" in r.headers["location"]
    assert P.load(db).notice_period_days == 90
    # invalid YAML edits are rejected, not saved
    r = client.post("/setup/profile", data={"csrf_token": csrf, "profile_yaml": "contact: 5"}, follow_redirects=False)
    assert "not+saved" in r.headers["location"]


# ---- tightened limitations ------------------------------------------------------------------------

def test_pdf_resume_output(profile, tmp_path):
    files = write_version(master_resume(profile), tmp_path, "r", pdf=True)
    assert Path(files["pdf"]).read_bytes()[:4] == b"%PDF"


def test_custom_skills_extend_the_taxonomy(tmp_path, monkeypatch):
    assert "Zanzibarql" not in extract_skills("Experience with ZanzibarQL is a plus")
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"custom_skills": {"Zanzibarql": ["zql"]}}))
    load_settings(tmp_path)
    assert "Zanzibarql" in extract_skills("Experience with ZanzibarQL is a plus")
    assert "Zanzibarql" in extract_skills("We use zql daily")


def test_second_resume_layout():
    prof, _ = parse_resume(str(ROOT / "data" / "sample" / "alt_layout_resume_SYNTHETIC.txt"))
    assert [(e.title, e.start, e.end) for e in prof.experience] == [
        ("Data Engineer", "2021-01", None), ("Junior Data Analyst", "2018-08", "2020-12")]
    assert prof.experience[0].employer == "Northwind Analytics (synthetic)"
    assert len(prof.experience[0].bullets) == 2 and prof.education[0].level == "master"
    assert {"Airflow", "Snowflake", "Tableau"} <= {s.name for s in prof.skills}
    assert prof.contact.email == "ravi.exampleperson@example.com"


# ---- front end: run the agent from the dashboard ---------------------------------------------------

def _wait(client, task_id, timeout=120):
    import time
    end = time.time() + timeout
    while time.time() < end:
        t = client.get(f"/tasks/{task_id}").json()
        if t["status"] in ("done", "failed"):
            return t
        time.sleep(0.3)
    raise AssertionError("task did not finish")


def _task_id(resp):
    loc = resp.headers["location"]
    assert "task=" in loc, loc
    return int(loc.split("task=")[1])


def test_dashboard_runs_the_whole_flow(env, reset_portal):
    from .conftest import sources as portal_sources
    s, db, prof = env
    s.sources = portal_sources(reset_portal.base_url, "1001")[:1]
    client = TestClient(create_app(s, db))
    page = client.get("/run")
    assert page.status_code == 200 and "Before you start" in page.text
    csrf = page.text.split('name="csrf_token" value="')[1].split('"')[0]
    assert client.post("/run/find", data={"csrf_token": "bad"}).status_code == 403

    t = _wait(client, _task_id(client.post("/run/check_sources", data={"csrf_token": csrf}, follow_redirects=False)))
    assert t["status"] == "done" and any("OK" in line for line in t["log"])
    assert db.one("SELECT COUNT(*) n FROM jobs")["n"] == 0  # checking stores nothing

    t = _wait(client, _task_id(client.post("/run/find", data={"csrf_token": csrf}, follow_redirects=False)))
    assert t["status"] == "done", t["log"]
    jid = job_id_by_employer(db, "Paymint")
    assert db.job(jid)["status"] == "READY_FOR_REVIEW"

    # dry run from the job page: fills the real form, never submits
    r = client.post(f"/jobs/{jid}/dry-run", data={"csrf_token": csrf}, follow_redirects=False)
    t = _wait(client, _task_id(r))
    assert t["status"] == "done" and any("Nothing was submitted" in line for line in t["log"])
    job_page = client.get(f"/jobs/{jid}")
    assert "Last dry run" in job_page.text and "ready_to_submit" in job_page.text
    assert client.get(f"/jobs/{jid}/dry-run.png").headers["content-type"] == "image/png"
    assert submissions(reset_portal) == []

    # approve, then submit only after switching to review mode
    client.post(f"/jobs/{jid}/approve", data={"csrf_token": csrf})
    assert client.post(f"/jobs/{jid}/submit", data={"csrf_token": csrf}).status_code == 400  # discovery mode
    assert client.post("/settings/mode", data={"mode": "auto", "csrf_token": csrf}).status_code == 400
    client.post("/settings/mode", data={"mode": "review", "csrf_token": csrf})
    assert s.mode == "review"
    t = _wait(client, _task_id(client.post(f"/jobs/{jid}/submit", data={"csrf_token": csrf}, follow_redirects=False)))
    assert any("submitted, reference TP-" in line for line in t["log"]), t["log"]
    assert db.job(jid)["status"] == "SUBMITTED" and len(submissions(reset_portal)) == 1
    assert "Recent runs" in client.get("/run").text


def test_one_run_at_a_time(env):
    import threading
    from jobagent.web import tasks
    s, db, prof = env
    gate = threading.Event()
    tasks.ACTIONS["_wait"] = (lambda t, db, s: gate.wait(10), "Waiting")
    try:
        runner = tasks.Runner(s)
        first = runner.start("_wait")
        with pytest.raises(RuntimeError):
            runner.start("_wait")
        gate.set()
        for _ in range(50):
            if first.status == "done":
                break
            import time
            time.sleep(0.05)
        assert first.status == "done"
    finally:
        tasks.ACTIONS.pop("_wait")
