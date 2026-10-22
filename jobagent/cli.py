"""Command line entry point: `python -m jobagent.cli <command>` (or `jobagent <command>` once installed)."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import yaml

from . import onboarding, pipeline, report
from . import profile as P
from .apply import engine, policy
from .config import load_settings
from .db import STATUSES, Database, loads
from .resume_parser import bullet_skill_gaps, parse_resume
from .security import generate_key
from .sources.base import CAPABILITIES

CONFIG_TEMPLATE = """# jobagent configuration. Secrets never go here: use environment variables (see .env.example).
mode: discovery            # discovery | review | auto
weights:                   # rubric weights (normalized to 100%)
  required_skills: 25
  responsibilities: 25
  experience: 20
  education: 10
  domain: 10
  preferred: 10
optimize_below: 50
target_min: 70
target_max: 80
max_job_age_days: 60
relevance_threshold: 0.25
per_host_min_interval: 1.0
extra_allowed_domains: []
pdf_output: true           # also write each resume version as PDF (needs Chromium)
upload_format: docx        # resume file uploaded to forms: docx or pdf
custom_skills: []          # skills the built-in taxonomy lacks, e.g. [Temporal, ClickHouse]; `setup` fills this
sources:
  # Greenhouse board token = the part after boards.greenhouse.io/
  # - {type: greenhouse, board_token: examplecompany, employer: Example Company}
  # - {type: lever, site: examplecompany, employer: Example Company}
  # - {type: ashby, organization: examplecompany, employer: Example Company}
  # - {type: workday, host: example.wd5.myworkdayjobs.com, tenant: example, site: External, employer: Example, search_text: backend}
  # - {type: careerpage, urls: [https://example.com/careers], employer: Example}
  # - {type: feed, url: https://example.com/jobs.rss, name: example-feed}
  # - {type: manual, path: my_jobs.yaml}   # jobs you found on LinkedIn / Indeed / referrals
"""


def _ctx(args):
    s = load_settings(args.home)
    return s, Database(s.db_path)


def _profile(db):
    return P.load(db)


def cmd_keygen(args):
    print(generate_key())


def cmd_init(args):
    s = load_settings(args.home)
    cfg = s.home / "config.yaml"
    if cfg.exists():
        print(f"{cfg} already exists")
    else:
        cfg.write_text(CONFIG_TEMPLATE)
        print(f"wrote {cfg}")
    Database(s.db_path)
    print(f"database at {s.db_path}")


def cmd_setup(args):
    """Guided first run: resume -> profile, personal details, boards, mode."""
    s = load_settings(args.home)
    cfg = s.home / "config.yaml"
    if not cfg.exists():
        cfg.write_text(CONFIG_TEMPLATE)
    db = Database(s.db_path)
    if args.template:
        Path(args.template).write_text(onboarding.details_template())
        print(f"wrote {args.template}: fill it in, then run `jobagent setup --resume <file> --details {args.template}`")
        return
    if args.resume:
        prof, _ = parse_resume(args.resume)
        print(f"read {args.resume}: {len(prof.experience)} roles, {sum(len(e.bullets) for e in prof.experience)} "
              f"bullets, {len(prof.education)} education entries, {len(prof.skills)} skills")
        for e in prof.experience:
            print(f"  - {e.title} at {e.employer} ({e.start} to {e.end or 'present'}, {len(e.bullets)} bullets)")
        try:
            old = P.load(db)
            prof = onboarding.merge_resume(old, prof)
            print("kept the personal details already saved")
        except LookupError:
            pass
    else:
        try:
            prof = P.load(db)
        except LookupError:
            sys.exit("give your resume: jobagent setup --resume path/to/resume.pdf (PDF with text, DOCX, TXT or MD)")
    boards: list = []
    mode = None
    if args.details:
        details = yaml.safe_load(Path(args.details).read_text()) or {}
        boards, mode = details.get("boards") or [], details.get("mode")
    elif sys.stdin.isatty() and not args.non_interactive:
        details, boards = onboarding.interview(prof)
    else:
        details = {}
    prof = onboarding.apply_details(prof, details)
    P.save(db, prof, actor="candidate (setup)")
    sources, problems = onboarding.boards_from_entries(boards)
    custom = onboarding.unknown_skills(prof)
    onboarding.update_config(cfg, sources=sources if boards else None, custom_skills=custom, mode=mode)
    enc = db.one("SELECT encrypted FROM profile WHERE id=1")["encrypted"]
    print(f"profile saved ({'encrypted' if enc else 'NOT encrypted: set JOBAGENT_ENCRYPTION_KEY and run setup again'})")
    if sources:
        print(f"{len(sources)} boards written to {cfg}:")
        for c in sources:
            print(f"  - {c['type']}: {c.get('board_token') or c.get('site') or c.get('organization') or c.get('url') or c.get('urls')}"
                  + (f" ({c['employer']})" if c.get("employer") else ""))
    for p in problems:
        print(f"  skipped: {p}")
    if custom:
        print("added to custom_skills so job ads mentioning them are matched: " + ", ".join(custom))
    gaps = bullet_skill_gaps(prof)
    if gaps:
        print("skills used in your bullets but not in your skills list (add only if accurate): " + ", ".join(gaps))
    todo = onboarding.unanswered(prof)
    if todo:
        print("still blank (applications that need these will pause for you): " + ", ".join(todo))
    print("Next: check the parsed resume with `jobagent profile show` or the dashboard's Setup page, then "
          "`jobagent check-sources`.")


def cmd_check_sources(args):
    """Fetch every configured source once and report, without storing anything."""
    from .http import PoliteClient
    from .sources.registry import build_sources
    s = load_settings(args.home)
    if not s.sources:
        sys.exit("no sources in config.yaml yet: run `jobagent setup` with your board URLs")
    http = PoliteClient(s.user_agent, s.http_timeout, s.http_retries, s.per_host_min_interval)
    ok = 0
    try:
        for cfg, src in zip(s.sources, build_sources(s.sources, http)):
            label = f"{cfg['type']}:{cfg.get('board_token') or cfg.get('site') or cfg.get('organization') or cfg.get('tenant') or cfg.get('url') or cfg.get('urls')}"
            try:
                jobs = list(src.fetch())
            except Exception as e:
                print(f"FAIL {label}: {type(e).__name__}: {str(e)[:200]}")
                continue
            ok += 1
            print(f"OK   {label}: {len(jobs)} jobs" + "".join(f"\n       {j.title} | {j.location or '?'} | {j.application_url}"
                                                        for j in jobs[:3]))
    finally:
        http.close()
    print(f"{ok}/{len(s.sources)} sources fetched. Nothing was stored.")


def cmd_llm_check(args):
    """One small real call to the Claude API to confirm the key and model work."""
    import logging
    import os
    from .llm import ClaudeLLM
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    s = load_settings(args.home)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set: the agent runs with deterministic templates (that is fine)")
    out = ClaudeLLM(s.anthropic_model, "low").json(
        "Reply with JSON only.", "Return ok=true.",
        {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False},
        max_tokens=200)
    if out and out.get("ok") is True:
        print(f"Claude API works with model {s.anthropic_model}")
    else:
        sys.exit(f"Claude API call failed (see the message above); the agent will fall back to templates")


def cmd_dry_run(args):
    s, db = _ctx(args)
    hold = None
    if args.headful:
        hold = lambda: input("The browser stays open so you can look at the filled form. Nothing is submitted. "
                             "Press Enter to close it... ")  # noqa: E731
    rep = engine.dry_run(db, s, _profile(db), args.job_id, headful=True if args.headful else None, hold=hold)
    print(f"{rep['outcome']}: {rep['detail']}")
    for f in rep["fields"]:
        print(f"  {f['action']:10} {f['field']}: {str(f['value'])[:70]}")
    for q in rep["open_questions"]:
        print(f"  OPEN       {q['question']}" + (f" ({q['note']})" if q.get("note") else ""))
    print(f"requests blocked because they could send data: {len(rep['blocked_requests'])}")
    print(f"report: {rep['report']}\nscreenshot: {rep['evidence'].get('screenshot', 'none')}\nNothing was submitted.")


def cmd_parse_resume(args):
    prof, _ = parse_resume(args.file)
    out = Path(args.out)
    out.write_text("# DRAFT parsed from your resume. Check every field, fill in the empty personal fields,\n"
                   "# set proficiency for each skill, then run: jobagent profile import " + str(out) + "\n"
                   + P.to_yaml(prof))
    print(f"wrote {out}: {len(prof.experience)} roles, {len(prof.education)} education entries, "
          f"{len(prof.skills)} skills, {sum(len(e.bullets) for e in prof.experience)} experience bullets")
    gaps = bullet_skill_gaps(prof)
    if gaps:
        print("skills used in your bullets but missing from the skills list (add them only if accurate): " + ", ".join(gaps))
    print("fields to fill in yourself: " + ", ".join(prof.missing_personal_fields()))


def cmd_import_profile(args):
    s, db = _ctx(args)
    prof = P.load_yaml(args.file)
    P.save(db, prof)
    enc = db.one("SELECT encrypted FROM profile WHERE id=1")["encrypted"]
    print(f"profile saved ({'encrypted' if enc else 'NOT encrypted: set JOBAGENT_ENCRYPTION_KEY'})"
          + (" [SYNTHETIC TEST DATA]" if prof.synthetic else ""))
    missing = prof.missing_personal_fields()
    if missing:
        print("still missing (applications needing these will pause for you): " + ", ".join(missing))


def cmd_show_profile(args):
    s, db = _ctx(args)
    print(P.to_yaml(_profile(db)))


def cmd_sources(args):
    for k, c in CAPABILITIES.items():
        print(f"{k:11} apply={c.apply_status:13} {c.name}\n            discovery: {c.discovery}\n            apply: {c.apply}"
              + (f"\n            note: {c.notes}" if c.notes else ""))


def cmd_discover(args):
    s, db = _ctx(args)
    print(json.dumps(pipeline.run_discovery(db, s), indent=2))


def cmd_process(args):
    s, db = _ctx(args)
    print(json.dumps(pipeline.screen_and_score(db, s, _profile(db)), indent=2))


def cmd_jobs(args):
    s, db = _ctx(args)
    for r in report.rows(db, args.status, args.search):
        print(f"{r['job_id']:4} {r['status']:22} base={r['baseline_score'] or '-':>5} final={r['final_score'] or '-':>5} "
              f"{r['title']} @ {r['employer']} ({r['location'] or '?'})" + (f"  !! {r['blocker'][:80]}" if r["blocker"] else ""))


def cmd_show(args):
    s, db = _ctx(args)
    job = db.job(args.job_id)
    if not job:
        sys.exit("no such job")
    app = db.application(args.job_id)
    final = db.latest_score(args.job_id, "final")
    out = {"job": {k: job[k] for k in ("id", "title", "employer", "location", "status", "application_url")},
           "screen": loads(job["screen_result"], {}),
           "scores": {"baseline": (db.latest_score(args.job_id, "baseline") or {"total": None})["total"],
                      "final": final["total"] if final else None},
           "breakdown": {k: v["score"] for k, v in loads(final["breakdown"], {}).get("categories", {}).items()} if final else None,
           "missing": loads(final["breakdown"], {}).get("missing_requirements") if final else None}
    if app:
        rv = db.resume_version(app["resume_version_id"])
        out["resume_version"] = {"label": rv["label"], "files": loads(rv["files"]), "changes": loads(rv["changes"])}
        out["answers"] = loads(app["answers"], {})
        out["blocker"] = app["blocker"]
        out["submission"] = {"submitted_at": app["submitted_at"], "verified": bool(app["verified"]),
                             "reference": app["reference"], "last_error": app["last_error"]}
    print(json.dumps(out, indent=2, default=str))


def cmd_answer(args):
    s, db = _ctx(args)
    pipeline.set_answer(db, args.job_id, args.question, args.value)
    print("saved")


def cmd_approve(args):
    s, db = _ctx(args)
    pipeline.approve(db, args.job_id)
    print(f"job {args.job_id} approved")


def cmd_apply(args):
    s, db = _ctx(args)
    if s.mode == "discovery":
        sys.exit("mode is 'discovery': nothing is submitted. Set mode to review (or auto with consent) in config.yaml")
    for r in pipeline.apply_pending(db, s, _profile(db), [args.job] if args.job else None):
        print(json.dumps(r, default=str))


def cmd_retry(args):
    s, db = _ctx(args)
    for r in pipeline.retry_failed(db, s, _profile(db)):
        print(json.dumps(r, default=str))


def cmd_run(args):
    s, db = _ctx(args)
    while True:
        prof = _profile(db)
        print(json.dumps({"discovery": pipeline.run_discovery(db, s),
                          "processing": pipeline.screen_and_score(db, s, prof),
                          "applications": pipeline.apply_pending(db, s, prof) if s.mode != "discovery" else [],
                          "retries": pipeline.retry_failed(db, s, prof) if s.mode != "discovery" else []},
                         indent=2, default=str))
        if not args.loop:
            break
        time.sleep(args.interval)


def cmd_consent(args):
    s, db = _ctx(args)
    if args.action == "show":
        c = db.active_consent()
        print(json.dumps(None if c is None else {"id": c["id"], "granted_at": c["granted_at"],
                                                 "constraints": loads(c["constraints"]), "statement": c["statement"]}, indent=2))
        return
    if args.action == "revoke":
        policy.revoke_auto_consent(db)
        print("auto-apply consent revoked")
        return
    constraints = {"allowed_sources": args.sources, "allowed_domains": args.domains, "min_final_score": args.min_score,
                   "allowed_apply_status": args.apply_status, "max_per_day": args.max_per_day}
    print(policy.CONSENT_STATEMENT)
    print("\nConstraints:\n" + yaml.safe_dump(constraints))
    if not args.yes:
        typed = input('Type "I AGREE" to grant auto-apply consent: ')
        if typed.strip() != "I AGREE":
            sys.exit("consent not granted")
    cid = policy.grant_auto_consent(db, constraints)
    print(f"consent #{cid} recorded. Set `mode: auto` in config.yaml to use it.")


def cmd_track(args):
    s, db = _ctx(args)
    if args.follow_up:
        db.update_job(args.job_id, follow_up_date=args.follow_up)
    if args.response:
        db.update_job(args.job_id, recruiter_response=args.response)
    if args.status:
        db.set_status(args.job_id, args.status, "candidate", args.response or "manual update")
    db.audit("candidate", "tracking_updated", "job", args.job_id, status=args.status, follow_up=args.follow_up,
             response=args.response)
    print("updated")


def cmd_report(args):
    s, db = _ctx(args)
    text = report.export(db, args.format)
    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {args.out}")
    else:
        print(text)


def cmd_audit(args):
    s, db = _ctx(args)
    sql = "SELECT * FROM audit_log" + (" WHERE entity='job' AND entity_id=?" if args.job else "") + " ORDER BY id"
    for r in db.q(sql, (args.job,) if args.job else ()):
        print(f"{r['ts']} {r['actor']:24} {r['action']:28} {r['entity'] or ''} {r['entity_id'] or ''} {r['detail']}")


def cmd_dashboard(args):
    import uvicorn
    from .web.app import create_app
    s, db = _ctx(args)
    uvicorn.run(create_app(s, db), host=args.host, port=args.port)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jobagent", description="Truthful job search and application agent")
    ap.add_argument("--home", help="data directory (default $JOBAGENT_HOME or ./var)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("keygen", help="print a new encryption key for JOBAGENT_ENCRYPTION_KEY").set_defaults(fn=cmd_keygen)
    sub.add_parser("init", help="create the data directory and config.yaml").set_defaults(fn=cmd_init)
    x = sub.add_parser("setup", help="guided first run: resume, personal details, boards")
    x.add_argument("--resume", help="your master resume (PDF with a text layer, DOCX, TXT or MD)")
    x.add_argument("--details", help="a filled-in details file (see --template)")
    x.add_argument("--template", metavar="FILE", help="write a blank details file to fill in, then stop")
    x.add_argument("--non-interactive", action="store_true", help="do not ask questions")
    x.set_defaults(fn=cmd_setup)
    sub.add_parser("check-sources", help="fetch each configured source once and report (stores nothing)"
                   ).set_defaults(fn=cmd_check_sources)
    sub.add_parser("llm-check", help="make one small Claude API call to confirm the key works").set_defaults(fn=cmd_llm_check)
    x = sub.add_parser("dry-run", help="fill a job's real application form without submitting, and screenshot it")
    x.add_argument("job_id", type=int)
    x.add_argument("--headful", action="store_true", help="show the browser and keep it open until you press Enter")
    x.set_defaults(fn=cmd_dry_run)
    p = sub.add_parser("profile", help="candidate profile")
    ps = p.add_subparsers(dest="pcmd", required=True)
    x = ps.add_parser("parse-resume", help="parse a master resume into a draft profile YAML")
    x.add_argument("file")
    x.add_argument("--out", default="profile.draft.yaml")
    x.set_defaults(fn=cmd_parse_resume)
    x = ps.add_parser("import", help="validate and store (encrypted) the verified profile YAML")
    x.add_argument("file")
    x.set_defaults(fn=cmd_import_profile)
    ps.add_parser("show").set_defaults(fn=cmd_show_profile)
    sub.add_parser("sources", help="source capability registry").set_defaults(fn=cmd_sources)
    sub.add_parser("discover", help="fetch jobs from configured sources").set_defaults(fn=cmd_discover)
    sub.add_parser("process", help="screen, score, optimize and prepare discovered jobs").set_defaults(fn=cmd_process)
    x = sub.add_parser("jobs", help="list jobs")
    x.add_argument("--status", choices=STATUSES)
    x.add_argument("--search")
    x.set_defaults(fn=cmd_jobs)
    x = sub.add_parser("show", help="show a job's scores, changes, answers and submission")
    x.add_argument("job_id", type=int)
    x.set_defaults(fn=cmd_show)
    x = sub.add_parser("answer", help="answer an application question for a job")
    x.add_argument("job_id", type=int)
    x.add_argument("question")
    x.add_argument("value")
    x.set_defaults(fn=cmd_answer)
    x = sub.add_parser("approve", help="approve a prepared application (review mode)")
    x.add_argument("job_id", type=int)
    x.set_defaults(fn=cmd_approve)
    x = sub.add_parser("apply", help="submit approved applications (or consented ones in auto mode)")
    x.add_argument("--job", type=int)
    x.set_defaults(fn=cmd_apply)
    sub.add_parser("retry", help="retry failed submissions whose retry time has come").set_defaults(fn=cmd_retry)
    x = sub.add_parser("run", help="discover + process + apply + retry (for cron)")
    x.add_argument("--loop", action="store_true")
    x.add_argument("--interval", type=int, default=3600)
    x.set_defaults(fn=cmd_run)
    x = sub.add_parser("consent", help="grant, show or revoke auto-apply consent")
    x.add_argument("action", choices=["grant", "show", "revoke"])
    x.add_argument("--sources", nargs="+", default=[])
    x.add_argument("--domains", nargs="+", default=[])
    x.add_argument("--min-score", type=float, default=70)
    x.add_argument("--apply-status", nargs="+", default=["tested_local"])
    x.add_argument("--max-per-day", type=int, default=5)
    x.add_argument("--yes", action="store_true", help="skip the typed confirmation (scripts/tests only)")
    x.set_defaults(fn=cmd_consent)
    x = sub.add_parser("track", help="record recruiter response, follow-up date or outcome")
    x.add_argument("job_id", type=int)
    x.add_argument("--status", choices=["INTERVIEW", "OFFER", "REJECTED", "CLOSED"])
    x.add_argument("--follow-up")
    x.add_argument("--response")
    x.set_defaults(fn=cmd_track)
    x = sub.add_parser("report", help="export the application report")
    x.add_argument("--format", choices=["csv", "json", "html"], default="csv")
    x.add_argument("--out")
    x.set_defaults(fn=cmd_report)
    x = sub.add_parser("audit", help="print the audit log")
    x.add_argument("--job", type=int)
    x.set_defaults(fn=cmd_audit)
    x = sub.add_parser("dashboard", help="run the local dashboard")
    x.add_argument("--host", default="127.0.0.1")
    x.add_argument("--port", type=int, default=8000)
    x.set_defaults(fn=cmd_dashboard)
    args = ap.parse_args(argv)
    try:
        args.fn(args)
    except (LookupError, ValueError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
