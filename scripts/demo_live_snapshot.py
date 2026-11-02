"""Discovery + screening + scoring on REAL job postings captured 2026-11-02, with the SYNTHETIC candidate.

The postings in data/live_snapshot_2026-11-02 are real (see build_snapshot.py for what is exact vs condensed).
They are served from a local stand-in for the Greenhouse and Lever APIs, because this cloud container's
network blocks those hosts. The real GreenhouseSource and LeverSource adapters parse them unchanged.
Nothing is submitted anywhere. Usage: python scripts/demo_live_snapshot.py <out_dir>
"""
from __future__ import annotations

import http.server
import json
import os
import re
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("JOBAGENT_LLM", "0")

from jobagent import pipeline, report  # noqa: E402
from jobagent import profile as P  # noqa: E402
from jobagent.config import load_settings  # noqa: E402
from jobagent.db import Database, loads  # noqa: E402
from jobagent.security import generate_key  # noqa: E402

SNAP = ROOT / "data" / "live_snapshot_2026-11-02"
GH = ["razorpay", "stripe", "databricks"]
LV = ["fampay", "cred", "zeta"]
NAMES = {"razorpay": "Razorpay", "stripe": "Stripe", "databricks": "Databricks", "fampay": "Fam (FamPay)", "cred": "CRED", "zeta": "Zeta"}


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        p = self.path.split("?")[0]
        m = re.match(r"/gh/v1/boards/(\w+)/jobs$", p) or re.match(r"/lever/v0/postings/(\w+)$", p)
        f = SNAP / (("greenhouse_" if p.startswith("/gh") else "lever_") + m.group(1) + ".json") if m else None
        if not f or not f.exists():
            self.send_response(404); self.end_headers(); return
        body = f.read_bytes()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):
        pass


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    os.environ["JOBAGENT_ENCRYPTION_KEY"] = generate_key()
    s = load_settings(Path(tempfile.mkdtemp(prefix="jobagent-live-")))
    s.per_host_min_interval = 0
    db = Database(s.db_path)
    prof = P.load_yaml(str(ROOT / "data/sample/profile_SYNTHETIC.yaml"))
    P.save(db, prof)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    b = f"http://127.0.0.1:{srv.server_address[1]}"
    src = [{"type": "greenhouse", "board_token": t, "employer": NAMES[t], "base_url": b + "/gh"} for t in GH] + \
          [{"type": "lever", "site": t, "employer": NAMES[t], "base_url": b + "/lever"} for t in LV]
    disc = pipeline.run_discovery(db, s, src)
    scored = pipeline.screen_and_score(db, s, prof)
    srv.shutdown()
    jobs = []
    for r in report.rows(db):
        job = db.one("SELECT * FROM jobs WHERE id=?", (r["job_id"],))
        app = db.application(r["job_id"])
        app = dict(app) if app else {}
        d = {"row": r, "job": {k: job[k] for k in job.keys() if k not in ("raw",)}}
        if app:
            d["app"] = {k: app[k] for k in app.keys()}
            if app.get("resume_version_id"):
                rv = db.resume_version(app["resume_version_id"])
                d["changes"] = loads(rv["changes"])
        sc = db.one("SELECT kind, total, breakdown FROM scores WHERE job_id=? ORDER BY id DESC LIMIT 1", (r["job_id"],))
        if sc:
            d["score"] = {"kind": sc["kind"], "total": sc["total"], "breakdown": loads(sc["breakdown"])}
        jobs.append(d)
    (out / "result.json").write_text(json.dumps({"discovery": disc, "screen": scored, "jobs": jobs,
                                                 "summary": report.summary(db)}, indent=1, default=str))
    print(json.dumps(disc, indent=1)); print(json.dumps(scored, indent=1))
    for j in jobs:
        r = j["row"]
        print(r["job_id"], r["employer"], "|", r.get("title"), "|", r["status"], r["baseline_score"], r["final_score"], "|",
              (r["disqualifiers"] or r["blocker"] or r["optimization_outcome"] or "")[:140])


if __name__ == "__main__":
    main(Path(sys.argv[1]))
