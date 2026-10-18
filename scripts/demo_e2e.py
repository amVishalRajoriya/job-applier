"""Sample end-to-end run against the LOCAL TEST ATS with SYNTHETIC candidate data.

Nothing here touches a real job site or a real person's data.
Usage: python scripts/demo_e2e.py [output_dir]
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("JOBAGENT_LLM", "0")

from jobagent import pipeline, report  # noqa: E402
from jobagent import profile as P  # noqa: E402
from jobagent.apply import policy  # noqa: E402
from jobagent.config import load_settings  # noqa: E402
from jobagent.db import Database, loads  # noqa: E402
from jobagent.resume_parser import parse_resume  # noqa: E402
from jobagent.security import generate_key  # noqa: E402
from testportal.server import PortalServer  # noqa: E402

LOG: list[str] = []


def log(line: str = "") -> None:
    print(line)
    LOG.append(line)


def main(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    os.environ["JOBAGENT_ENCRYPTION_KEY"] = generate_key()  # throwaway key for the demo database
    home = Path(tempfile.mkdtemp(prefix="jobagent-demo-"))
    s = load_settings(home)
    s.per_host_min_interval = 0
    db = Database(s.db_path)
    log("# Sample end-to-end run (SYNTHETIC TEST DATA, local test ATS only)\n")

    log("## 1. Parse the master resume")
    draft, _ = parse_resume(ROOT / "data/sample/master_resume_SYNTHETIC.md")
    log(f"Parsed {len(draft.experience)} roles, {sum(len(e.bullets) for e in draft.experience)} bullets, "
        f"{len(draft.skills)} skills. Fields left for the candidate: {', '.join(draft.missing_personal_fields())}")
    prof = P.load_yaml(str(ROOT / "data/sample/profile_SYNTHETIC.yaml"))
    P.save(db, prof)
    log("Imported the verified synthetic profile (encrypted at rest).\n")

    with PortalServer() as portal:
        b = portal.base_url
        src = [
            {"type": "greenhouse", "board_token": "all", "employer": "unused", "base_url": b + "/gh"},
            {"type": "lever", "site": "shopwell", "employer": "Shopwell (synthetic)", "base_url": b + "/lever"},
            {"type": "ashby", "organization": "visionary", "employer": "Visionary (synthetic)", "base_url": b + "/ashby"},
            {"type": "workday", "host": "paymint.example", "tenant": "paymint", "site": "External",
             "employer": "Paymint (synthetic)", "base_url": b + "/wd"},
            {"type": "careerpage", "urls": [b + "/careers/paymint"]},
            {"type": "feed", "url": b + "/feed.xml", "name": "rss"},
        ]
        log("## 2. Discover")
        log("```\n" + json.dumps(pipeline.run_discovery(db, s, src), indent=2) + "\n```")
        log("## 3. Screen, score, optimize, prepare")
        log("```\n" + json.dumps(pipeline.screen_and_score(db, s, prof), indent=2) + "\n```")
        log("| Job | Employer | Status | Baseline | Final | Outcome / reason |\n|---|---|---|---|---|---|")
        for r in report.rows(db):
            why = r["optimization_outcome"] or r["disqualifiers"] or r["blocker"] or ""
            log(f"| {r['job_id']} | {r['employer']} | {r['status']} | {r['baseline_score'] or ''} | "
                f"{r['final_score'] or ''} | {why[:110]} |")

        shop = db.one("SELECT id FROM jobs WHERE employer LIKE 'Shopwell%'")["id"]
        rv = db.resume_version(db.application(shop)["resume_version_id"])
        ch = loads(rv["changes"])
        log(f"\n### Optimization detail: job {shop} (Shopwell, synthetic)\n")
        log(f"Baseline {ch['baseline_score']} -> final {ch['final_score']} ({ch['outcome']}).")
        for c in ch["changes"]:
            log(f"- {c['kind']} {c['target']}: {c['reason']}" + (f" (+{c['score_delta']})" if c.get("score_delta") else "")
                + (f"\n  - after: {c['after']}" if c.get("after") and c["kind"] != "reorder_skills" else ""))
        log("Remaining gaps (not invented): " + "; ".join(ch["remaining_gaps"][:8]))
        shutil.copy(loads(rv["files"])["docx"], out_dir / f"sample_tailored_resume_job{shop}_SYNTHETIC.docx")
        shutil.copy(loads(rv["files"])["txt"], out_dir / f"sample_tailored_resume_job{shop}_SYNTHETIC.txt")
        Path(out_dir / f"sample_cover_letter_job{shop}_SYNTHETIC.txt").write_text(db.application(shop)["cover_letter"])

        log("\n## 4. Review mode: candidate answers the unexpected question and approves")
        s.mode = "review"
        led = db.one("SELECT id FROM jobs WHERE employer LIKE 'Ledgerly%'")
        if led:
            q = "Describe your experience maintaining COBOL mainframe batch systems"
            pipeline.set_answer(db, led["id"], q, "I have not worked with COBOL mainframe systems.")
            log(f"Answered for job {led['id']}: {q!r}")
        for r in db.q("SELECT id FROM jobs WHERE status='READY_FOR_REVIEW'"):
            try:
                pipeline.approve(db, r["id"], actor="candidate (demo)")
            except ValueError as e:
                log(f"job {r['id']} not approved: {e}")
        log("\n## 5. Apply")
        for res in pipeline.apply_pending(db, s, prof):
            log(f"- job {res['job_id']}: **{res['outcome']}** -> {res['status']}"
                + (f", reference {res.get('reference')}" if res.get("reference") else "")
                + (f" ({res['detail']})" if res.get("detail") else ""))

        log("\n## 6. Duplicate protection")
        done = db.one("SELECT id FROM jobs WHERE status='SUBMITTED' ORDER BY id LIMIT 1")["id"]
        from jobagent.apply import engine
        log(f"Re-submitting job {done}: {engine.submit(db, s, prof, done)['outcome']}")

        log("\n## 7. Auto-apply needs explicit consent")
        s.mode = "auto"
        cap = db.one("SELECT id FROM jobs WHERE employer LIKE 'Captchaco%'")["id"]
        log(f"Without consent, job {cap}: {engine.submit(db, s, prof, cap)['detail']}")
        policy.grant_auto_consent(db, {"allowed_sources": ["greenhouse"], "allowed_domains": ["127.0.0.1"], "min_final_score": 60, "max_per_day": 50})
        log(f"With consent, CAPTCHA job {cap}: {engine.submit(db, s, prof, cap)['detail']}")

    for fmt in ("csv", "json", "html"):
        (out_dir / f"application_report_SYNTHETIC.{fmt}").write_text(report.export(db, fmt))
    log("\n## 8. Final summary")
    log("```\n" + json.dumps(report.summary(db), indent=2) + "\n```")
    log(f"\nAudit log entries: {db.one('SELECT COUNT(*) n FROM audit_log')['n']}")
    (out_dir / "RUN_LOG.md").write_text("\n".join(LOG) + "\n")
    print(f"\nwrote {out_dir}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "sample_run")
