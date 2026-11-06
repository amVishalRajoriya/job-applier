"""Connectors for ATS public job-board APIs: Greenhouse, Lever, Ashby, Workday."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from ..security import html_to_text
from .base import JobRecord, Source, infer_remote_policy, normalize_employment_type, parse_salary, text_inferred


def _ms_to_iso(ms: int | float | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()


class GreenhouseSource(Source):
    """https://developers.greenhouse.io/job-board.html (public, no auth)."""

    kind = "greenhouse"

    def fetch(self) -> Iterable[JobRecord]:
        base = self.config.get("base_url", "https://boards-api.greenhouse.io").rstrip("/")
        token = self.config["board_token"]
        employer = self.config.get("employer") or token
        data = self.http.get_json(f"{base}/v1/boards/{token}/jobs", params={"content": "true"})
        for j in data.get("jobs", []):
            desc = html_to_text(j.get("content"))
            location = (j.get("location") or {}).get("name")
            meta = {m.get("name", "").lower(): m.get("value") for m in j.get("metadata") or [] if isinstance(m, dict)}
            smin, smax, cur = parse_salary(desc)
            yield JobRecord(
                source=self.kind, source_id=f"{token}:{j['id']}", title=j.get("title", "").strip(),
                employer=j.get("company_name") or employer, description=desc, location=location,
                remote_policy=infer_remote_policy(location, j.get("title"), desc[:600]),
                employment_type=normalize_employment_type(str(meta.get("employment type") or "") or desc[:800]),
                date_posted=(j.get("first_published") or j.get("updated_at") or "")[:10] or None,
                original_url=j.get("absolute_url"), application_url=j.get("absolute_url"),
                salary_min=smin, salary_max=smax, salary_currency=cur,
                raw={"id": j.get("id"), "board": token,
                     **text_inferred(smax is not None, not meta.get("employment type"))},
            )


class LeverSource(Source):
    """https://github.com/lever/postings-api (public, no auth)."""

    kind = "lever"

    def fetch(self) -> Iterable[JobRecord]:
        base = self.config.get("base_url", "https://api.lever.co").rstrip("/")
        site = self.config["site"]
        employer = self.config.get("employer") or site
        data = self.http.get_json(f"{base}/v0/postings/{site}", params={"mode": "json"})
        for p in data:
            cats = p.get("categories") or {}
            req, pref, sections = [], [], []
            for lst in p.get("lists") or []:
                heading = (lst.get("text") or "").lower()
                items = [x.strip("- ").strip() for x in html_to_text(lst.get("content")).splitlines() if x.strip("- ").strip()]
                sections.append(f"{lst.get('text')}\n" + "\n".join(f"- {i}" for i in items))
                if any(k in heading for k in ("nice", "bonus", "preferred", "plus")):
                    pref.extend(items)
                elif any(k in heading for k in ("require", "qualif", "you have", "what you bring", "must")):
                    req.extend(items)
            desc = "\n\n".join(x for x in [p.get("descriptionPlain") or html_to_text(p.get("description")),
                                          *sections, p.get("additionalPlain") or html_to_text(p.get("additional"))] if x)
            sal = p.get("salaryRange") or {}
            workplace = (p.get("workplaceType") or "").lower()
            yield JobRecord(
                source=self.kind, source_id=f"{site}:{p['id']}", title=(p.get("text") or "").strip(),
                employer=employer, description=desc, location=cats.get("location"),
                remote_policy={"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on-site": "onsite"}.get(workplace)
                or infer_remote_policy(cats.get("location"), desc[:600]),
                required_qualifications=req, preferred_qualifications=pref,
                employment_type=normalize_employment_type(cats.get("commitment")),
                date_posted=_ms_to_iso(p.get("createdAt")),
                original_url=p.get("hostedUrl"), application_url=p.get("applyUrl") or p.get("hostedUrl"),
                salary_min=sal.get("min"), salary_max=sal.get("max"), salary_currency=sal.get("currency"),
                salary_interval=(sal.get("interval") or "").replace("per-", "") or None,
                raw={"id": p.get("id"), "site": site},
            )


class AshbySource(Source):
    """https://developers.ashbyhq.com/docs/public-job-posting-api (public, no auth)."""

    kind = "ashby"

    def fetch(self) -> Iterable[JobRecord]:
        base = self.config.get("base_url", "https://api.ashbyhq.com").rstrip("/")
        org = self.config["organization"]
        employer = self.config.get("employer") or org
        data = self.http.get_json(f"{base}/posting-api/job-board/{org}", params={"includeCompensation": "true"})
        for j in data.get("jobs", []):
            if j.get("isListed") is False:
                continue
            desc = j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml"))
            comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
            smin, smax, cur = parse_salary(comp)
            workplace = (j.get("workplaceType") or "").lower()
            remote = "remote" if j.get("isRemote") or workplace == "remote" else (
                "hybrid" if workplace == "hybrid" else "onsite" if workplace in ("onsite", "on-site") else None)
            jid = j.get("id") or j.get("jobUrl")
            yield JobRecord(
                source=self.kind, source_id=f"{org}:{jid}", title=(j.get("title") or "").strip(), employer=employer,
                description=desc, location=j.get("location"), remote_policy=remote,
                employment_type=normalize_employment_type(j.get("employmentType")),
                date_posted=(j.get("publishedAt") or "")[:10] or None,
                original_url=j.get("jobUrl"), application_url=j.get("applyUrl") or j.get("jobUrl"),
                salary_min=smin, salary_max=smax, salary_currency=cur, raw={"id": jid, "org": org},
            )


class WorkdaySource(Source):
    """Workday career sites expose a public JSON search endpoint used by their own front end."""

    kind = "workday"

    def fetch(self) -> Iterable[JobRecord]:
        host = self.config["host"]  # e.g. company.wd5.myworkdayjobs.com
        tenant, site = self.config["tenant"], self.config["site"]
        base = self.config.get("base_url", f"https://{host}").rstrip("/")
        employer = self.config.get("employer") or tenant
        limit = int(self.config.get("page_size", 20))
        max_jobs = int(self.config.get("max_jobs", 100))
        search = self.config.get("search_text", "")
        offset, seen = 0, 0
        while seen < max_jobs:
            page = self.http.post_json(f"{base}/wday/cxs/{tenant}/{site}/jobs",
                                       {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": search})
            postings = page.get("jobPostings") or []
            if not postings:
                break
            for p in postings:
                seen += 1
                path = p.get("externalPath")
                if not path:
                    continue
                detail = self.http.get_json(f"{base}/wday/cxs/{tenant}/{site}{path}")
                info = detail.get("jobPostingInfo") or {}
                desc = html_to_text(info.get("jobDescription"))
                public_url = info.get("externalUrl") or f"https://{host}/{site}{path}"
                smin, smax, cur = parse_salary(desc)
                yield JobRecord(
                    source=self.kind, source_id=f"{tenant}:{info.get('jobReqId') or info.get('id') or path}",
                    title=(info.get("title") or p.get("title") or "").strip(),
                    employer=(detail.get("hiringOrganization") or {}).get("name") or employer,
                    description=desc, location=info.get("location") or p.get("locationsText"),
                    remote_policy=infer_remote_policy(info.get("remoteType"), info.get("location"), desc[:600]),
                    employment_type=normalize_employment_type(info.get("timeType")),
                    date_posted=(info.get("startDate") or "")[:10] or None,
                    closing_date=(info.get("endDate") or "")[:10] or None,
                    original_url=public_url, application_url=public_url,
                    salary_min=smin, salary_max=smax, salary_currency=cur,
                    raw={"path": path, "canApply": info.get("canApply"), **text_inferred(smax is not None)},
                )
            offset += limit
            if offset >= int(page.get("total") or 0):
                break
