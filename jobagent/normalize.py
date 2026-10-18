"""Job normalization and cross-source deduplication."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .db import Database, dumps
from .sources.base import JobRecord

TRACKING = re.compile(r"^(utm_.*|gh_src|gh_jid_src|source|src|ref|refid|trk|lever-source.*|lever-origin|"
                      r"mode|iis|iisn|ccuid|fbclid|gclid|codes)$", re.I)
TITLE_SUBS = [
    (r"\bsr\.?\b", "senior"), (r"\bjr\.?\b", "junior"), (r"\bengg\b|\bengr\b", "engineer"),
    (r"\bswe\b", "software engineer"), (r"\bsde\b", "software engineer"), (r"\bdev\b", "developer"),
    (r"\bmgr\b", "manager"), (r"\bii\b", "2"), (r"\biii\b", "3"), (r"\bi\b", "1"),
]
CITY_ALIASES = {"bangalore": "bengaluru", "bombay": "mumbai", "gurgaon": "gurugram", "nyc": "new york",
                "new york city": "new york", "sf": "san francisco", "calcutta": "kolkata", "madras": "chennai"}
LEGAL = re.compile(r"\b(inc|incorporated|llc|ltd|limited|pvt|private|plc|gmbh|corp|corporation|co|company|"
                   r"technologies|technology|labs)\b\.?", re.I)


def canonical_url(url: str | None) -> str | None:
    if not url:
        return None
    p = urlparse(url.strip())
    if not p.scheme:
        return None
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False) if not TRACKING.match(k)]
    path = re.sub(r"/(apply|application)/?$", "", p.path.rstrip("/")) or "/"
    return urlunparse(("https", (p.hostname or "").lower(), path, "", urlencode(sorted(q)), ""))


def norm_title(title: str) -> str:
    t = title.lower()
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", t)          # "(Remote)", "[REQ-123]"
    t = re.sub(r"\b(remote|hybrid|onsite|on-site)\b", " ", t)
    for rx, rep in TITLE_SUBS:
        t = re.sub(rx, rep, t)
    t = re.sub(r"[^a-z0-9+# ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def norm_employer(name: str) -> str:
    t = LEGAL.sub(" ", name.lower())
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def norm_location(loc: str | None, remote: str | None = None) -> str:
    if not loc:
        return "remote" if remote == "remote" else ""
    first = re.split(r"[,;/|(-]", loc.lower())[0].strip()
    if "remote" in loc.lower() and (not first or first == "remote"):
        return "remote"
    return CITY_ALIASES.get(first, first)


def dedup_key(rec: JobRecord) -> str:
    basis = "|".join([norm_employer(rec.employer), norm_title(rec.title), norm_location(rec.location, rec.remote_policy)])
    return hashlib.sha1(basis.encode()).hexdigest()


_MERGE_FIELDS = ["location", "remote_policy", "description", "experience_requirement", "salary_min", "salary_max",
                 "salary_currency", "salary_interval", "employment_type", "date_posted", "closing_date",
                 "original_url", "application_url"]


def upsert_job(db: Database, rec: JobRecord) -> tuple[int, bool]:
    """Insert or merge a discovered job. Returns (job_id, created)."""
    existing_src = db.one("SELECT job_id FROM job_sources WHERE source=? AND source_id=?", (rec.source, rec.source_id))
    canon = canonical_url(rec.original_url)
    key = dedup_key(rec)
    job_id = existing_src["job_id"] if existing_src else None
    if job_id is None and canon:
        row = db.one("SELECT id FROM jobs WHERE canonical_url=?", (canon,))
        job_id = row["id"] if row else None
    if job_id is None:
        row = db.one("SELECT id FROM jobs WHERE dedup_key=?", (key,))
        job_id = row["id"] if row else None

    created = False
    if job_id is None:
        with db.tx() as c:
            cur = c.execute(
                """INSERT INTO jobs(dedup_key, canonical_url, title, employer, location, remote_policy, description,
                required_qualifications, preferred_qualifications, experience_requirement, salary_min, salary_max,
                salary_currency, salary_interval, employment_type, date_posted, closing_date, original_url,
                application_url, discovered_at, status, flags, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'DISCOVERED', '[]', ?)""",
                (key, canon, rec.title, rec.employer, rec.location, rec.remote_policy, rec.description,
                 dumps(rec.required_qualifications), dumps(rec.preferred_qualifications), rec.experience_requirement,
                 rec.salary_min, rec.salary_max, rec.salary_currency, rec.salary_interval, rec.employment_type,
                 rec.date_posted, rec.closing_date, rec.original_url, rec.application_url, rec.discovered_at,
                 rec.discovered_at))
            job_id = int(cur.lastrowid)
        created = True
        db.audit("discovery", "job_discovered", "job", job_id, source=rec.source, source_id=rec.source_id,
                 title=rec.title, employer=rec.employer)
    else:
        row = db.job(job_id)
        updates = {}
        for f in _MERGE_FIELDS:
            new = getattr(rec, f)
            if new and not row[f]:
                updates[f] = new
        # prefer the longer description and an ATS application URL over a board URL
        if rec.description and len(rec.description) > len(row["description"] or "") * 1.2:
            updates["description"] = rec.description
        if rec.source in ("greenhouse", "lever", "ashby", "workday", "testportal") and rec.application_url:
            updates["application_url"] = rec.application_url
        for f, attr in (("required_qualifications", rec.required_qualifications),
                        ("preferred_qualifications", rec.preferred_qualifications)):
            if attr and not (row[f] and row[f] != "[]"):
                updates[f] = attr
        if updates:
            db.update_job(job_id, **updates)
    if not existing_src:
        with db.tx() as c:
            c.execute("INSERT OR IGNORE INTO job_sources(job_id, source, source_id, url, discovered_at, raw) VALUES (?,?,?,?,?,?)",
                      (job_id, rec.source, rec.source_id, rec.original_url, rec.discovered_at, dumps(rec.raw)))
        if not created:
            db.audit("discovery", "duplicate_merged", "job", job_id, source=rec.source, source_id=rec.source_id)
    return job_id, created
