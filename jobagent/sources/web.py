"""Connectors for employer career pages (schema.org JobPosting), RSS/JSON feeds and manual imports."""
from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable

import yaml

from ..security import html_to_text
from .base import JobRecord, Source, infer_remote_policy, normalize_employment_type, parse_salary

_LDJSON = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)


def _iter_jobpostings(node: Any) -> Iterable[dict]:
    if isinstance(node, list):
        for n in node:
            yield from _iter_jobpostings(n)
    elif isinstance(node, dict):
        t = node.get("@type")
        if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
            yield node
        for key in ("@graph", "itemListElement", "item"):
            if key in node:
                yield from _iter_jobpostings(node[key])


def _place(loc: Any) -> str | None:
    if isinstance(loc, list):
        names = [_place(x) for x in loc]
        return "; ".join(n for n in names if n) or None
    if isinstance(loc, dict):
        addr = loc.get("address") or {}
        if isinstance(addr, str):
            return addr
        parts = [addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")]
        parts = [p.get("name") if isinstance(p, dict) else p for p in parts]
        return ", ".join(p for p in parts if p) or loc.get("name")
    return loc if isinstance(loc, str) else None


def jobposting_to_record(jp: dict, source: str, page_url: str, employer_hint: str | None) -> JobRecord:
    org = jp.get("hiringOrganization") or {}
    employer = (org.get("name") if isinstance(org, dict) else org) or employer_hint or "Unknown employer"
    desc = html_to_text(jp.get("description"))
    ident = jp.get("identifier")
    ident = ident.get("value") if isinstance(ident, dict) else ident
    url = jp.get("url") or page_url
    sal = jp.get("baseSalary") or {}
    val = sal.get("value") if isinstance(sal, dict) else None
    smin = smax = None
    cur = sal.get("currency") if isinstance(sal, dict) else None
    interval = None
    if isinstance(val, dict):
        smin, smax = val.get("minValue") or val.get("value"), val.get("maxValue") or val.get("value")
        interval = (val.get("unitText") or "").lower() or None
    if smin is None:
        smin, smax, cur = parse_salary(desc)
    remote = "remote" if jp.get("jobLocationType") == "TELECOMMUTE" else infer_remote_policy(desc[:600], jp.get("title"))
    emp = jp.get("employmentType")
    emp = " ".join(emp) if isinstance(emp, list) else emp
    quals = jp.get("qualifications")
    required = [q for q in (quals if isinstance(quals, list) else [quals]) if q] if quals else []
    sid = str(ident or hashlib.sha1(url.encode()).hexdigest()[:16])
    return JobRecord(
        source=source, source_id=f"{employer}:{sid}", title=(jp.get("title") or "").strip(), employer=employer,
        description=desc, location=_place(jp.get("jobLocation")) or ("Remote" if remote == "remote" else None),
        remote_policy=remote, required_qualifications=[html_to_text(q) for q in required],
        experience_requirement=_experience(jp.get("experienceRequirements")),
        employment_type=normalize_employment_type(emp), date_posted=(jp.get("datePosted") or "")[:10] or None,
        closing_date=(jp.get("validThrough") or "")[:10] or None, original_url=url,
        application_url=jp.get("applicationUrl") or url,
        salary_min=smin, salary_max=smax, salary_currency=cur, salary_interval=interval,
        raw={"page": page_url},
    )


def _experience(v: Any) -> str | None:
    if not v:
        return None
    if isinstance(v, dict):
        months = v.get("monthsOfExperience")
        try:
            return f"{int(float(months)) // 12}+ years" if months else (html_to_text(v.get("description")) or None)
        except (TypeError, ValueError):
            return None
    return html_to_text(str(v))


class CareerPageSource(Source):
    """Reads schema.org JobPosting JSON-LD from employer career pages (robots.txt respected)."""

    kind = "careerpage"

    def fetch(self) -> Iterable[JobRecord]:
        for url in self.config.get("urls", []):
            page = self.http.get_text(url, check_robots=True)
            for block in _LDJSON.findall(page):
                try:
                    data = json.loads(block.strip())
                except json.JSONDecodeError:
                    continue
                for jp in _iter_jobpostings(data):
                    yield jobposting_to_record(jp, self.kind, url, self.config.get("employer"))


class FeedSource(Source):
    """RSS 2.0 (<item>) or JSON feed (list of job objects or JSON Feed 1.1)."""

    kind = "feed"

    def fetch(self) -> Iterable[JobRecord]:
        url = self.config["url"]
        text = self.http.get_text(url, check_robots=True)
        employer_default = self.config.get("employer")
        if text.lstrip().startswith("<"):
            root = ET.fromstring(text)
            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                desc = html_to_text(item.findtext("description"))
                employer = item.findtext("{*}company") or item.findtext("company") or employer_default
                if not employer and " at " in title:
                    title, employer = title.rsplit(" at ", 1)
                guid = item.findtext("guid") or link
                loc = item.findtext("{*}location") or item.findtext("location")
                smin, smax, cur = parse_salary(desc)
                yield JobRecord(source=self.kind, source_id=f"{self.name}:{guid}", title=title.strip(),
                                employer=(employer or "Unknown employer").strip(), description=desc, location=loc,
                                remote_policy=infer_remote_policy(title, loc, desc[:600]),
                                date_posted=None, original_url=link, application_url=link,
                                employment_type=normalize_employment_type(desc[:600]),
                                salary_min=smin, salary_max=smax, salary_currency=cur, raw={"feed": url})
        else:
            data = json.loads(text)
            items = data.get("items", data.get("jobs", [])) if isinstance(data, dict) else data
            for it in items:
                if it.get("@type") == "JobPosting":
                    yield jobposting_to_record(it, self.kind, url, employer_default)
                    continue
                yield _record_from_dict(it, self.kind, f"{self.name}:", employer_default)


def _record_from_dict(it: dict, source: str, prefix: str, employer_default: str | None) -> JobRecord:
    desc = it.get("description") or it.get("content_text") or html_to_text(it.get("content_html"))
    url = it.get("url") or it.get("original_url")
    sid = it.get("id") or url or hashlib.sha1((it.get("title", "") + desc[:200]).encode()).hexdigest()[:16]
    smin, smax, cur = it.get("salary_min"), it.get("salary_max"), it.get("salary_currency")
    if smin is None:
        smin, smax, cur = parse_salary(desc)
    return JobRecord(
        source=source, source_id=f"{prefix}{sid}", title=(it.get("title") or "").strip(),
        employer=(it.get("employer") or it.get("company") or employer_default or "Unknown employer").strip(),
        description=desc, location=it.get("location"),
        remote_policy=it.get("remote_policy") or infer_remote_policy(it.get("location"), desc[:600]),
        required_qualifications=it.get("required_qualifications") or [],
        preferred_qualifications=it.get("preferred_qualifications") or [],
        employment_type=normalize_employment_type(it.get("employment_type") or desc[:600]),
        date_posted=(it.get("date_posted") or it.get("date_published") or "")[:10] or None,
        closing_date=it.get("closing_date"), original_url=url,
        application_url=it.get("application_url") or url,
        salary_min=smin, salary_max=smax, salary_currency=cur, raw={"imported": True},
    )


class ManualSource(Source):
    """Jobs the candidate found elsewhere (e.g. LinkedIn, Indeed, referrals), from a YAML/JSON file."""

    kind = "manual"

    def fetch(self) -> Iterable[JobRecord]:
        path = Path(self.config["path"])
        data = yaml.safe_load(path.read_text())
        for it in data if isinstance(data, list) else data.get("jobs", []):
            origin = it.get("origin", "manual")
            yield _record_from_dict(it, self.kind, f"{origin}:", None)
