"""Job source interface, normalized job record and the source capability registry."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from ..http import PoliteClient


@dataclass
class JobRecord:
    source: str
    source_id: str
    title: str
    employer: str
    description: str = ""
    location: str | None = None
    remote_policy: str | None = None  # remote | hybrid | onsite | None (unknown)
    required_qualifications: list[str] = field(default_factory=list)
    preferred_qualifications: list[str] = field(default_factory=list)
    experience_requirement: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_interval: str | None = None
    employment_type: str | None = None
    date_posted: str | None = None
    closing_date: str | None = None
    original_url: str | None = None
    application_url: str | None = None
    discovered_at: str = field(default_factory=lambda: datetime.now(timezone.utc).replace(microsecond=0).isoformat())
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Capability:
    name: str
    discovery: str          # how jobs are found
    access: str             # official API / public feed / manual
    apply: str              # how applications are completed
    apply_status: str       # tested_local | untested_live | manual_only | unsupported
    domains: tuple[str, ...]  # legitimate application destinations
    notes: str = ""


CAPABILITIES: dict[str, Capability] = {
    "greenhouse": Capability(
        "Greenhouse job boards", "Public Job Board API (GET /v1/boards/{token}/jobs?content=true)",
        "official public API, no auth", "hosted application form via browser adapter (generic_form)",
        "untested_live", ("greenhouse.io", "job-boards.greenhouse.io", "boards.greenhouse.io"),
        "Parsing tested against recorded-shape fixtures. Live form submission has not been verified; "
        "keep Greenhouse jobs in review mode until a supervised live run succeeds."),
    "lever": Capability(
        "Lever postings", "Public Postings API (GET /v0/postings/{site}?mode=json)",
        "official public API, no auth", "hosted application form via browser adapter (generic_form)",
        "untested_live", ("lever.co", "jobs.lever.co", "jobs.eu.lever.co"),
        "Parsing tested against fixtures. Live submission not verified."),
    "ashby": Capability(
        "Ashby job boards", "Public Job Posting API (GET /posting-api/job-board/{org})",
        "official public API, no auth", "manual (Ashby forms are React apps with anti-bot checks)",
        "manual_only", ("ashbyhq.com", "jobs.ashbyhq.com")),
    "workday": Capability(
        "Workday career sites", "Public CXS search endpoint used by the career site (POST /wday/cxs/{tenant}/{site}/jobs)",
        "public listings, no auth", "manual: Workday requires a candidate account and often MFA",
        "manual_only", ("myworkdayjobs.com", "myworkdaysite.com"),
        "Discovery only. The agent never creates Workday accounts or logs in."),
    "careerpage": Capability(
        "Company career pages", "schema.org JobPosting JSON-LD embedded in career pages",
        "public pages; robots.txt respected", "depends on employer; manual unless the URL is a supported ATS",
        "manual_only", ()),
    "feed": Capability(
        "RSS / JSON job feeds", "RSS 2.0 or JSON feeds published by boards and employers",
        "public feeds", "manual unless the URL is a supported ATS", "manual_only", ()),
    "manual": Capability(
        "Manual import (LinkedIn, Indeed, referrals)", "candidate pastes URL + description, or imports a YAML/JSON file",
        "candidate-provided", "manual unless the URL is a supported ATS", "manual_only", ()),
    "linkedin": Capability(
        "LinkedIn Jobs", "not automated: LinkedIn's User Agreement prohibits scraping and automated activity; "
        "no public job-search API for individuals", "manual import only", "manual only (Easy Apply is never automated)",
        "unsupported", ("linkedin.com",)),
    "indeed": Capability(
        "Indeed", "not automated: Indeed retired its public job-search API and its terms prohibit scraping",
        "manual import only", "manual only", "unsupported", ("indeed.com",)),
    "testportal": Capability(
        "Local test ATS", "Greenhouse/Lever/Ashby/Workday-shaped APIs served locally",
        "local", "generic_form browser adapter", "tested_local", ("127.0.0.1", "localhost"),
        "Used by tests and the demo. Never a real employer."),
}


class Source:
    """Base class: subclasses implement fetch() and yield JobRecords."""

    kind = "base"

    def __init__(self, http: PoliteClient, **config: Any):
        self.http = http
        self.config = config

    @property
    def name(self) -> str:
        return self.config.get("name") or self.kind

    def fetch(self) -> Iterable[JobRecord]:  # pragma: no cover - interface
        raise NotImplementedError


# ---- shared parsing helpers -------------------------------------------------------

_REMOTE = re.compile(r"\b(remote|work from home|wfh|distributed)\b", re.I)
_HYBRID = re.compile(r"\bhybrid\b", re.I)
_ONSITE = re.compile(r"\b(on-?site|in[- ]office|in person)\b", re.I)


def infer_remote_policy(*texts: str | None) -> str | None:
    blob = " ".join(t for t in texts if t)
    if _HYBRID.search(blob):
        return "hybrid"
    if _REMOTE.search(blob) and not re.search(r"\bnot (a )?remote\b|\bno remote\b", blob, re.I):
        return "remote"
    if _ONSITE.search(blob):
        return "onsite"
    return None


_EMP_TYPES = {"full-time": r"full[- ]?time|permanent|fulltime",
              "part-time": r"part[- ]?time", "contract": r"contract|contractor|freelance|temporary|temp\b",
              "internship": r"intern(ship)?"}


def normalize_employment_type(text: str | None) -> str | None:
    if not text:
        return None
    for k, rx in _EMP_TYPES.items():
        if re.search(rx, text, re.I):
            return k
    return None


_SAL = re.compile(r"(?P<cur>[$€£₹]|usd|eur|gbp|inr)?\s?(?P<a>\d[\d,.]*)\s?(?P<ka>k|lpa|l)?\s*(?:-|–|to)\s*"
                  r"(?P<cur2>[$€£₹]|usd|eur|gbp|inr)?\s?(?P<b>\d[\d,.]*)\s?(?P<kb>k|lpa|l)?", re.I)
_CUR = {"$": "USD", "usd": "USD", "€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP", "₹": "INR", "inr": "INR"}


def parse_salary(text: str | None) -> tuple[float | None, float | None, str | None]:
    """Best-effort salary range from free text (only when a currency marker is present)."""
    if not text:
        return None, None, None
    for m in _SAL.finditer(text):
        cur = m.group("cur") or m.group("cur2")
        unit = (m.group("ka") or m.group("kb") or "").lower()
        if not cur and unit not in ("lpa", "l"):
            continue
        def num(s: str) -> float:
            return float(s.replace(",", ""))
        try:
            a, b = num(m.group("a")), num(m.group("b"))
        except ValueError:
            continue
        mult = 1000 if unit == "k" else 100000 if unit in ("lpa", "l") else 1
        currency = _CUR.get((cur or "").lower(), "INR" if unit in ("lpa", "l") else None)
        return a * mult, b * mult, currency
    return None, None, None
