"""LinkedIn, Indeed and Naukri.com: discovery without scraping.

These three sites forbid scraping and automated access in their terms of use, put most pages behind a
login wall, and run CAPTCHAs and bot detection. Accounts that look automated get restricted or banned.
So this module never requests anything from them. It reads only what the candidate already has:

* job-alert emails the sites send to the candidate (saved .eml files, an .mbox export, or read-only
  IMAP access to the candidate's own mailbox),
* job pages the candidate saved from their own browser (File > Save page as: .html, .htm or .mhtml),
* job URLs the candidate pasted, which wait in a queue until their page is saved (by hand, or with the
  opt-in supervised `jobagent capture` command, see jobagent/capture.py).

Applying on these sites always stays with the candidate (see the "assisted" apply status): the agent
prepares the documents and answers, opens the job in the candidate's own browser, and the candidate
submits. Nothing here can be switched to auto-submit.

Status: untested_live. The parsers were written against SYNTHETIC fixtures shaped after the public
structure of these pages and emails (schema.org JobPosting JSON-LD, known URL patterns). Real pages and
emails change often; check `jobagent check-sources` output on real files before relying on it.
"""
from __future__ import annotations

import email
import email.policy
import hashlib
import imaplib
import json
import mailbox
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import parseaddr
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import parse_qs, unquote, urlsplit

from ..security import html_to_text
from .base import JobRecord, Source, infer_remote_policy, normalize_employment_type, parse_salary, text_inferred
from .web import _LDJSON, _iter_jobpostings, jobposting_to_record

JOB_SITES = ("linkedin", "indeed", "naukri")
SITE_NAMES = {"linkedin": "LinkedIn", "indeed": "Indeed", "naukri": "Naukri.com"}
# Hosts the agent must never drive with a browser or HTTP client. Used by the apply policy as a hard stop.
NEVER_AUTOMATE = ("linkedin.com", "indeed.com", "naukri.com", "naukrigulf.com")
# Senders whose alert emails are trusted to describe jobs. Anything else is ignored unless the
# source is configured with require_known_sender: false (e.g. alerts forwarded from another address).
ALERT_SENDER_DOMAINS = {
    "linkedin": ("linkedin.com",),
    "indeed": ("indeed.com", "indeedemail.com"),
    "naukri": ("naukri.com", "naukri.co", "infoedge.com"),
}


# ---- URL recognition ---------------------------------------------------------------------------

@dataclass(frozen=True)
class JobRef:
    site: str
    job_id: str
    url: str          # canonical public job URL

    @property
    def source_id(self) -> str:
        return f"{self.site}:{self.job_id}"


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _on(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


_WRAPPER_PARAMS = ("url", "q", "u", "target", "dest", "destination", "redirect", "redirect_url", "redirectUrl", "r")


def unwrap(url: str, depth: int = 4) -> str:
    """Follow redirect wrappers that mail clients add (Google, Outlook safelinks, tracking hops)
    by reading the wrapped URL from the query string. Nothing is fetched."""
    for _ in range(depth):
        if _identify_direct(url):
            return url  # a real job link that merely carries a url=... tracking parameter: keep it
        try:
            parts = urlsplit(url)
        except ValueError:
            return url
        qs = parse_qs(parts.query)
        nxt = None
        for p in _WRAPPER_PARAMS:
            for v in qs.get(p, []):
                v = unquote(v) if v.startswith("http%3A") or v.startswith("https%3A") else v
                if v.startswith(("http://", "https://")):
                    nxt = v
                    break
            if nxt:
                break
        if not nxt or nxt == url:
            return url
        url = nxt
    return url


_LI_VIEW = re.compile(r"/(?:comm/)?jobs/view/(?:[^/?#]*?-)?(\d{6,})")
_INDEED_JK = re.compile(r"^[0-9a-f]{16}$")
_NAUKRI_LISTING = re.compile(r"/job-listings-(?:([a-z0-9-]+?)-)?(\d{9,15})(?:[/?#]|$)", re.I)
_NAUKRIGULF_JID = re.compile(r"-jid-(\d{9,15})(?:[/?#]|$)", re.I)
_INDEED_COUNTRY_HOST = re.compile(r"^[a-z]{2}\.indeed\.com$")
_INDEED_TLD_HOST = re.compile(r"^(?:www\.)?(indeed\.[a-z.]+)$")


def identify(url: str | None) -> JobRef | None:
    """Recognise a LinkedIn, Indeed or Naukri job URL (as given, or after unwrapping redirects)."""
    if not url:
        return None
    url = url.strip()
    return _identify_direct(url) or _identify_direct(unwrap(url))


def _indeed_base(host: str) -> str:
    """The public job host for an Indeed link: www.indeed.com, a country subdomain (in.indeed.com) or a country
    domain (www.indeed.co.in). Click-tracking hosts (cts., engage., click. ...) map to www.indeed.com."""
    if _INDEED_COUNTRY_HOST.match(host) and not host.startswith("www."):
        return host
    m = _INDEED_TLD_HOST.match(host)
    if m:
        return "www." + m.group(1)
    return "www.indeed.com"


def _identify_direct(url: str) -> JobRef | None:
    host = _host(url)
    if not host:
        return None
    parts = urlsplit(url)
    qs = parse_qs(parts.query)
    if _on(host, "linkedin.com"):
        m = _LI_VIEW.search(parts.path)
        jid = m.group(1) if m else next((v for v in qs.get("currentJobId", []) if v.isdigit()), None)
        if jid:
            return JobRef("linkedin", jid, f"https://www.linkedin.com/jobs/view/{jid}/")
        return None
    if _on(host, "indeed.com") or re.search(r"(^|\.)indeed\.[a-z.]+$", host):
        jk = next((v for k in ("jk", "vjk") for v in qs.get(k, []) if _INDEED_JK.match(v.lower())), None)
        if jk:
            return JobRef("indeed", jk.lower(), f"https://{_indeed_base(host)}/viewjob?jk={jk.lower()}")
        return None
    if _on(host, "naukri.com"):
        m = _NAUKRI_LISTING.search(parts.path + ("?" if parts.query else ""))
        if m:
            slug = f"{m.group(1).lower()}-" if m.group(1) else ""
            return JobRef("naukri", m.group(2), f"https://www.naukri.com/job-listings-{slug}{m.group(2)}")
        jid = next((v for k in ("jobId", "jobid", "jid") for v in qs.get(k, []) if v.isdigit()), None)
        if jid:
            return JobRef("naukri", jid, f"https://www.naukri.com/job-listings-{jid}")
        return None
    if _on(host, "naukrigulf.com"):
        m = _NAUKRIGULF_JID.search(parts.path)
        if m:
            return JobRef("naukri", f"gulf-{m.group(1)}", f"https://www.naukrigulf.com{parts.path}")
        return None
    return None


def site_of(url: str | None) -> str | None:
    """'linkedin' | 'indeed' | 'naukri' for any URL on those sites (job page or not), else None."""
    host = _host(unwrap(url or ""))
    if not host:
        return None
    if _on(host, "linkedin.com"):
        return "linkedin"
    if _on(host, "indeed.com") or re.search(r"(^|\.)indeed\.[a-z.]+$", host):
        return "indeed"
    if _on(host, "naukri.com") or _on(host, "naukrigulf.com"):
        return "naukri"
    return None


# ---- a tiny DOM-less HTML reader (stdlib only) ---------------------------------------------------

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_BLOCK = {"p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "br", "section", "table", "ul", "ol"}


class _Reader(HTMLParser):
    """Collects meta tags, the <title>, and the text of elements matching simple selectors,
    plus a flat list of (text, href) runs used for alert emails."""

    def __init__(self, selectors: dict[str, list[tuple[str, str, str]]]):
        super().__init__(convert_charrefs=True)
        self.selectors = selectors   # name -> [(tag or "*", attr, substring)]
        self.meta: dict[str, str] = {}
        self.title = ""
        self.found: dict[str, list[str]] = {}
        self.runs: list[tuple[str, str | None]] = []
        self._stack: list[tuple[str, dict[str, list[str]], str | None]] = []  # (tag, {name: text buffer}, href)
        self._in_title = False
        self._skip = 0

    def _matches(self, tag: str, attrs: dict[str, str]) -> list[str]:
        out = []
        for name, sels in self.selectors.items():
            for t, attr, sub in sels:
                if t not in ("*", tag):
                    continue
                val = attrs.get(attr)
                if val is not None and (sub == "" or sub in val):
                    out.append(name)
                    break
        return out

    def _newline(self) -> None:
        if self.runs and self.runs[-1] != ("\n", None):
            self.runs.append(("\n", None))
        for _, bufs, _ in self._stack:
            for b in bufs.values():
                b.append("\n")

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag in ("script", "style", "noscript", "template"):
            self._skip += 1
            return
        if tag == "meta":
            key = a.get("property") or a.get("name")
            if key and "content" in a:
                self.meta.setdefault(key.lower(), a["content"])
            return
        if tag == "title":
            self._in_title = True
        if tag in _BLOCK:
            self._newline()
        if tag in _VOID:
            return
        href = a.get("href") if tag == "a" else (self._stack[-1][2] if self._stack else None)
        self._stack.append((tag, {n: [] for n in self._matches(tag, a)}, href))

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template"):
            self._skip = max(0, self._skip - 1)
            return
        if tag == "title":
            self._in_title = False
        if tag in _VOID:
            return
        for i in range(len(self._stack) - 1, -1, -1):   # pop to the matching tag (tolerates unclosed tags)
            if self._stack[i][0] == tag:
                while len(self._stack) > i:
                    _, bufs, _ = self._stack.pop()
                    for n, b in bufs.items():
                        self.found.setdefault(n, []).append("".join(b))
                break
        if tag in _BLOCK:
            self._newline()

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
        for _, bufs, _ in self._stack:
            for b in bufs.values():
                b.append(data)
        text = re.sub(r"\s+", " ", data)
        if text.strip():
            self.runs.append((text, self._stack[-1][2] if self._stack else None))


def _read(html: str, selectors: dict[str, list[tuple[str, str, str]]] | None = None) -> _Reader:
    r = _Reader(selectors or {})
    try:
        r.feed(html)
        r.close()
    except Exception:  # malformed markup: keep what was read
        pass
    return r


def _first(r: _Reader, name: str) -> str | None:
    for t in r.found.get(name, []):
        lines = [re.sub(r"\s+", " ", x).strip() for x in t.split("\n")]
        t = "\n".join(x for x in lines if x)
        if t:
            return t
    return None


# ---- saved job pages ---------------------------------------------------------------------------

class PageBlocked(ValueError):
    """The saved page is a login wall, CAPTCHA or bot check instead of a job."""


_BLOCKED = re.compile(
    r"authwall|join linkedin to see|sign in to view (this|more) job|"
    r"verify (that )?you are (a )?human|are you a robot|just a moment\.\.\.|checking your browser|"
    r"additional verification required|unusual (traffic|activity)|access denied|"
    r"please (log ?in|login) to (view|continue)|security check", re.I)

# Fallback selectors when a page has no JobPosting JSON-LD. Class names on these sites are hashed or
# renamed often; keep matches loose (substring of class / data-testid) and expect them to break.
_PAGE_SELECTORS = {
    "linkedin": {
        "title": [("h1", "class", "top-card-layout__title"), ("h1", "class", "topcard__title"),
                  ("h1", "class", "job-details-jobs-unified-top-card__job-title"), ("h1", "class", "")],
        "company": [("a", "class", "topcard__org-name-link"), ("*", "class", "topcard__org-name"),
                    ("*", "class", "job-details-jobs-unified-top-card__company-name")],
        "location": [("span", "class", "topcard__flavor--bullet"),
                     ("*", "class", "job-details-jobs-unified-top-card__primary-description")],
        "description": [("div", "class", "show-more-less-html__markup"), ("div", "class", "description__text"),
                        ("div", "id", "job-details"), ("*", "class", "jobs-description__content")],
        "criteria": [("ul", "class", "description__job-criteria-list")],
    },
    "indeed": {
        "title": [("h1", "class", "jobsearch-JobInfoHeader-title"), ("*", "data-testid", "jobsearch-JobInfoHeader-title"),
                  ("h1", "class", "")],
        "company": [("*", "data-testid", "inlineHeader-companyName"), ("*", "data-company-name", ""),
                    ("*", "class", "jobsearch-CompanyInfoContainer")],
        "location": [("*", "data-testid", "inlineHeader-companyLocation"), ("*", "data-testid", "job-location"),
                     ("*", "data-testid", "jobsearch-JobInfoHeader-companyLocation")],
        "description": [("div", "id", "jobDescriptionText")],
        "salary": [("*", "id", "salaryInfoAndJobType"), ("*", "data-testid", "jobsearch-OtherJobDetailsContainer")],
    },
    "naukri": {
        "title": [("h1", "class", "jd-header-title"), ("h1", "class", "")],
        "company": [("*", "class", "jd-header-comp-name"), ("a", "class", "comp-name")],
        "location": [("*", "class", "location"), ("*", "class", "loc")],
        "experience": [("*", "class", "exp")],
        "salary": [("*", "class", "salary")],
        "description": [("*", "class", "dang-inner-html"), ("section", "class", "job-desc"),
                        ("div", "class", "job-desc")],
        "skills": [("*", "class", "key-skill")],
    },
}


def _canonical_from_page(r: _Reader) -> str | None:
    for key in ("og:url", "twitter:url"):
        if r.meta.get(key):
            return r.meta[key]
    return None


_APPLY_TEXT = re.compile(r"\bapply\b", re.I)


def _external_ats_url(r: _Reader, candidates: Iterable[str | None] = ()) -> str | None:
    """An application link that points at a supported employer ATS (Greenhouse, Lever), taken only from the
    posting's own applicationUrl or from a link whose text says "apply". Nothing is fetched or followed."""
    from ..apply.policy import capability_for
    urls = [c for c in candidates if c] + [href for text, href in r.runs if href and _APPLY_TEXT.search(text)]
    for u in urls:
        u = unwrap(u)
        if capability_for(u)[0] in ("greenhouse", "lever"):
            return u
    return None


def parse_job_page(html: str, url: str | None = None, *, origin: str = "saved_page") -> JobRecord:
    """Parse one saved LinkedIn/Indeed/Naukri job page. Raises PageBlocked or ValueError."""
    r = _read(html, None)
    ref = identify(url) or identify(_canonical_from_page(r))
    if ref is None:
        for m in re.finditer(r'https?://[^\s"\'<>]+', html[:200000]):
            ref = identify(m.group(0).replace("&amp;", "&"))
            if ref:
                break
    if ref is None:
        raise ValueError("not a recognisable LinkedIn, Indeed or Naukri job page (no job URL found in it)")

    postings = []
    for block in _LDJSON.findall(html):
        try:
            postings.extend(_iter_jobpostings(json.loads(block.strip())))
        except json.JSONDecodeError:
            continue
    if postings:
        rec = jobposting_to_record(postings[0], ref.site, ref.url, None)
    else:
        sel = _read(html, _PAGE_SELECTORS[ref.site])
        title = _first(sel, "title") or (sel.meta.get("og:title") or sel.title or "").split("|")[0].strip()
        desc = _first(sel, "description") or html_to_text(sel.meta.get("og:description") or "")
        if _BLOCKED.search(sel.title + " " + " ".join(t for t, _ in sel.runs[:400])) and len(desc) < 200:
            raise PageBlocked(f"{SITE_NAMES[ref.site]} showed a login wall or bot check instead of the job; "
                              "open it in your browser while signed in, then save the page again")
        if not title or not desc:
            raise ValueError(f"could not find the job title and description on this {SITE_NAMES[ref.site]} page")
        company = _first(sel, "company")
        if not company and " at " in (sel.meta.get("og:title") or ""):
            company = sel.meta["og:title"].split(" at ", 1)[1].split("|")[0].strip()
        location = _first(sel, "location")
        extra = [x for x in (_first(sel, "experience"), _first(sel, "salary"), _first(sel, "criteria"),
                             ("Key skills: " + _first(sel, "skills")) if _first(sel, "skills") else None) if x]
        full = "\n\n".join([desc, *extra])
        smin, smax, cur = parse_salary(_first(sel, "salary") or full)
        inferred = text_inferred(smax is not None and not _first(sel, "salary"), True)
        rec = JobRecord(source=ref.site, source_id=ref.source_id, title=title, employer=company or "Unknown employer",
                        description=full, location=location,
                        remote_policy=infer_remote_policy(location, title, full[:800]),
                        employment_type=normalize_employment_type(full[:1500]),
                        salary_min=smin, salary_max=smax, salary_currency=cur,
                        experience_requirement=_first(sel, "experience"))
    if _BLOCKED.search(rec.title or "") or not rec.title:
        raise PageBlocked(f"{SITE_NAMES[ref.site]} page has no job title (login wall or bot check?)")
    rec.source = ref.site
    rec.source_id = ref.source_id
    rec.original_url = ref.url
    ats = _external_ats_url(r, [postings[0].get("applicationUrl") if postings else None])
    rec.application_url = ats or ref.url
    inferred = inferred if not postings else {k: v for k, v in rec.raw.items() if k.endswith("_from_text")}
    rec.raw = {"site": ref.site, "job_id": ref.job_id, "origin": origin, "untested_live": True,
               **({"external_ats": ats} if ats else {}), **inferred}
    return rec


def read_saved_file(path: Path) -> tuple[str, str | None]:
    """(html, url hint) from a saved .html/.htm or a single-file .mhtml/.mht page."""
    data = path.read_bytes()
    if path.suffix.lower() in (".mhtml", ".mht"):
        msg = email.message_from_bytes(data, policy=email.policy.compat32)
        url = msg.get("Snapshot-Content-Location") or msg.get("Content-Location")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True) or b""
                return _decode(payload, part.get_content_charset()), url or part.get("Content-Location")
        raise ValueError("no HTML part in the .mhtml file")
    html = data.decode("utf-8", "replace")
    # Chrome and Firefox add <!-- saved from url=(0071)https://... --> to "Save page as" files
    m = re.search(r"<!--\s*saved from url=\(\d+\)(\S+)\s*-->", html[:2000])
    return html, m.group(1) if m else None


# ---- job-alert emails --------------------------------------------------------------------------

_GENERIC_LINK = re.compile(r"^(view( job| details| all)?|apply( now)?|easy apply|see (all|more)( jobs)?|"
                           r"save( job)?|more jobs|unsubscribe|search|jobs?|new|promoted|actively recruiting|"
                           r"\d+ (new )?jobs?.*|be an early applicant|view similar jobs)$", re.I)
_NOISE = re.compile(r"^(easy apply|promoted|actively recruiting|new|be an early applicant|\d+ (applicants?|connections?).*|"
                    r"\d+ (days?|hours?|minutes?) ago|just posted|today|apply now|view job|save|·|\||-)$", re.I)


@dataclass
class AlertJob:
    ref: JobRef
    title: str
    company: str | None = None
    location: str | None = None
    snippet: list[str] = field(default_factory=list)


def _sender_site(msg: Message) -> str | None:
    addr = parseaddr(str(msg.get("From", "")))[1].lower()
    host = addr.rsplit("@", 1)[-1] if "@" in addr else ""
    for site, domains in ALERT_SENDER_DOMAINS.items():
        if any(_on(host, d) for d in domains):
            return site
    return None


def _decode(payload: bytes, charset: str | None) -> str:
    """Decode with the declared charset; unknown or bogus charsets (unknown-8bit, x-user-defined, typos)
    fall back to UTF-8 with replacement characters instead of failing."""
    try:
        return payload.decode(charset or "utf-8", "replace")
    except (LookupError, TypeError):
        return payload.decode("utf-8", "replace")


def _html_parts(msg: Message) -> tuple[list[str], list[str]]:
    html, text = [], []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype not in ("text/html", "text/plain") or part.get_filename():
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        s = _decode(payload, part.get_content_charset())
        (html if ctype == "text/html" else text).append(s)
    return html, text


def _split_company_location(line: str) -> tuple[str | None, str | None]:
    for sep in (" · ", " • ", " | ", " - ", " – "):
        if sep in line:
            a, b = line.split(sep, 1)
            return a.strip() or None, b.strip() or None
    return line.strip() or None, None


_LOCATIONISH = re.compile(r"remote|hybrid|on-?site|india|bengaluru|bangalore|mumbai|delhi|pune|hyderabad|chennai|"
                          r"gurgaon|gurugram|noida|kolkata|ahmedabad|,\s*[A-Z]{2}\b|united|kingdom|states", re.I)
_EXPISH = re.compile(r"\b\d+\s*(-|to)\s*\d+\s*(yrs?|years?)\b|\b\d+\+?\s*(yrs?|years?)\b", re.I)


def jobs_from_alert_html(html: str) -> list[AlertJob]:
    r = _read(html)
    jobs: dict[str, AlertJob] = {}
    order: list[str] = []
    current: AlertJob | None = None
    after: list[str] = []

    def close():
        if current is None:
            return
        lines = [x for x in (re.sub(r"\s+", " ", y).strip() for y in "".join(after).split("\n")) if x and not _NOISE.match(x)]
        lines = [x for x in lines if x.lower() != current.title.lower()]
        if lines and not current.company:
            company, loc = _split_company_location(lines.pop(0))
            current.company = company
            current.location = current.location or loc
        for x in list(lines):
            if not current.location and len(x) < 80 and _LOCATIONISH.search(x) and not _EXPISH.search(x):
                current.location = x
                lines.remove(x)
        current.snippet.extend(lines[:6])

    for text, href in r.runs:
        ref = identify(href) if href else None
        if ref:
            t = text.strip()
            if ref.source_id not in jobs:
                close()
                current, after = AlertJob(ref, ""), []
                jobs[ref.source_id] = current
                order.append(ref.source_id)
            elif current is None or current.ref.source_id != ref.source_id:
                close()
                current, after = jobs[ref.source_id], []
            if t and not _GENERIC_LINK.match(t) and not current.title:
                current.title = t
            elif t and current.title and t != current.title and not _GENERIC_LINK.match(t) and not current.company:
                company, loc = _split_company_location(t)   # some alerts link the company line too
                current.company, current.location = company, current.location or loc
            continue
        if href and current is not None and identify(href) is None and _host(unwrap(href)):
            # a link to something else (another site, an unsubscribe page) ends this job's block
            close()
            current, after = None, []
            continue
        if current is not None:
            after.append(text)
    close()
    return [jobs[k] for k in order if jobs[k].title]


def jobs_from_alert_text(text: str) -> list[AlertJob]:
    out: dict[str, AlertJob] = {}
    lines = [x.strip() for x in text.splitlines()]
    for i, line in enumerate(lines):
        for m in re.finditer(r"https?://\S+", line):
            ref = identify(m.group(0).rstrip(">).,"))
            if not ref or ref.source_id in out:
                continue
            before = [x for x in lines[max(0, i - 3):i] if x and not re.match(r"https?://", x) and not _NOISE.match(x)]
            title = line[:m.start()].strip(" :-<") or (before[0] if before else "")
            company, loc = _split_company_location(before[1]) if len(before) > 1 else (None, None)
            if title:
                out[ref.source_id] = AlertJob(ref, title, company, loc)
    return list(out.values())


def parse_alert_email(raw: bytes | str | Message, require_known_sender: bool = True) -> tuple[str | None, list[AlertJob]]:
    """(site of the sender, jobs) for one alert email. Jobs on a site other than the sender's are dropped
    when require_known_sender is on, so a forged or unrelated email cannot inject listings."""
    msg = raw if isinstance(raw, Message) else (
        email.message_from_bytes(raw, policy=email.policy.compat32) if isinstance(raw, bytes)
        else email.message_from_string(raw, policy=email.policy.compat32))
    site = _sender_site(msg)
    if require_known_sender and site is None:
        return None, []
    html, text = _html_parts(msg)
    found: dict[str, AlertJob] = {}
    for h in html:
        for j in jobs_from_alert_html(h):
            found.setdefault(j.ref.source_id, j)
    if not found:
        for t in text:
            for j in jobs_from_alert_text(t):
                found.setdefault(j.ref.source_id, j)
    jobs = [j for j in found.values() if not require_known_sender or j.ref.site == site]
    return site, jobs


def alert_to_record(j: AlertJob, received: str | None) -> JobRecord:
    snippet = "\n".join(j.snippet)
    note = (f"[From a {SITE_NAMES[j.ref.site]} job-alert email: only a short summary is available. Save the job "
            "page from your browser into your saved-pages folder for the full description and a better score.]")
    smin, smax, cur = parse_salary(snippet)
    exp = _EXPISH.search(snippet)
    return JobRecord(
        source=j.ref.site, source_id=j.ref.source_id, title=j.title.strip(),
        employer=(j.company or "Unknown employer").strip(), description=(snippet + "\n\n" + note).strip(),
        location=j.location, remote_policy=infer_remote_policy(j.location, j.title, snippet),
        experience_requirement=exp.group(0) if exp else None, employment_type=normalize_employment_type(snippet),
        date_posted=received, original_url=j.ref.url, application_url=j.ref.url,
        salary_min=smin, salary_max=smax, salary_currency=cur,
        raw={"site": j.ref.site, "job_id": j.ref.job_id, "origin": "alert_email", "summary_only": True,
             "untested_live": True, **text_inferred(smax is not None, True)},
    )


def _received(msg: Message) -> str | None:
    try:
        return email.utils.parsedate_to_datetime(msg.get("Date")).date().isoformat()
    except Exception:
        return None


# ---- sources -----------------------------------------------------------------------------------

def _expand(p: str | os.PathLike) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(p))))


class JobAlertSource(Source):
    """Job-alert emails from LinkedIn, Indeed and Naukri that the candidate already receives.

    config:
      folder: directory of saved .eml files (and/or .mbox files)          } at least one
      imap: {host, user, password_env, mailbox: INBOX, since_days: 14}     }
      sites: [linkedin, indeed, naukri]    (default: all three)
      require_known_sender: true           (only trust mail from the sites' own domains)
    IMAP is opened read-only (EXAMINE): nothing is marked read, moved or deleted.
    """

    kind = "job_alerts"

    def __init__(self, http, **config):
        super().__init__(http, **config)
        self.problems: list[str] = []

    def messages(self) -> Iterator[Message]:
        if self.config.get("folder"):
            folder = _expand(self.config["folder"])
            if not folder.is_dir():
                raise FileNotFoundError(f"job alert folder not found: {folder}")
            for p in sorted(folder.iterdir()):
                suf = p.suffix.lower()
                try:
                    if suf == ".eml":
                        yield email.message_from_bytes(p.read_bytes(), policy=email.policy.compat32)
                    elif suf in (".mbox", ".mbx"):
                        for m in mailbox.mbox(str(p)):
                            yield m
                except (OSError, ValueError) as e:  # one unreadable file must not lose the rest of the folder
                    self.problems.append(f"{p.name}: {type(e).__name__}: {e}")
        if self.config.get("imap"):
            yield from self._imap(self.config["imap"])

    def _imap(self, cfg: dict) -> Iterator[Message]:
        pw = os.environ.get(cfg.get("password_env", "JOBAGENT_IMAP_PASSWORD"))
        if not pw:
            raise RuntimeError(f"set {cfg.get('password_env', 'JOBAGENT_IMAP_PASSWORD')} to an app password for "
                               f"{cfg.get('user')} (never put it in config.yaml)")
        cls = self.config.get("_imap_class") or imaplib.IMAP4_SSL
        conn = cls(cfg["host"], int(cfg.get("port", 993)))
        try:
            conn.login(cfg["user"], pw)
            conn.select(cfg.get("mailbox", "INBOX"), readonly=True)
            since = (datetime.now(timezone.utc) - timedelta(days=int(cfg.get("since_days", 14)))).strftime("%d-%b-%Y")
            ids: list[bytes] = []
            sites = self.config.get("sites") or list(JOB_SITES)
            for site in sites:
                for dom in ALERT_SENDER_DOMAINS[site]:
                    typ, data = conn.search(None, "SINCE", since, "FROM", dom)
                    if typ == "OK" and data and data[0]:
                        ids.extend(data[0].split())
            for mid in dict.fromkeys(ids):
                typ, data = conn.fetch(mid, "(BODY.PEEK[])")
                if typ == "OK" and data and isinstance(data[0], tuple):
                    yield email.message_from_bytes(data[0][1], policy=email.policy.compat32)
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def fetch(self) -> Iterable[JobRecord]:
        sites = set(self.config.get("sites") or JOB_SITES)
        strict = bool(self.config.get("require_known_sender", True))
        seen: set[str] = set()
        self.problems = []
        for msg in self.messages():
            try:
                _, jobs = parse_alert_email(msg, require_known_sender=strict)
            except Exception as e:  # one malformed message must not lose every other alert in the run
                self.problems.append(f"alert email {msg.get('Subject', '')!r}: {type(e).__name__}: {e}")
                continue
            for j in jobs:
                if j.ref.site in sites and j.ref.source_id not in seen:
                    seen.add(j.ref.source_id)
                    yield alert_to_record(j, _received(msg))


class SavedPagesSource(Source):
    """Job pages the candidate saved from their own browser, plus a queue of pasted job URLs.

    config:
      folder: directory of saved pages (.html, .htm, .mhtml, .mht)
      urls_file: optional text file, one LinkedIn/Indeed/Naukri job URL per line. URLs whose page has
                 not been saved yet are listed by `jobagent check-sources` and `jobagent capture`.
    Pages that turn out to be a login wall or CAPTCHA are reported, not stored.
    """

    kind = "saved_pages"

    def __init__(self, http, **config):
        super().__init__(http, **config)
        self.problems: list[str] = []
        self.pending: list[JobRef] = []

    def fetch(self) -> Iterable[JobRecord]:
        self.problems, self.pending = [], []
        folder = _expand(self.config["folder"])
        if not folder.is_dir():
            raise FileNotFoundError(f"saved pages folder not found: {folder}")
        got: set[str] = set()
        for p in sorted(folder.iterdir()):
            if p.suffix.lower() not in (".html", ".htm", ".mhtml", ".mht"):
                continue
            try:
                html, url = read_saved_file(p)
                rec = parse_job_page(html, url)
            except (PageBlocked, ValueError) as e:
                self.problems.append(f"{p.name}: {e}")
                continue
            except Exception as e:  # unreadable file, odd encoding, ...: report it and keep going
                self.problems.append(f"{p.name}: {type(e).__name__}: {e}")
                continue
            if rec.source_id in got:
                continue
            got.add(rec.source_id)
            rec.raw["file"] = p.name
            yield rec
        for ref in queued_urls(self.config.get("urls_file")):
            if ref.source_id not in got:
                self.pending.append(ref)


def queued_urls(path: str | os.PathLike | None) -> list[JobRef]:
    if not path:
        return []
    p = _expand(path)
    if not p.exists():
        return []
    out: dict[str, JobRef] = {}
    for line in p.read_text().splitlines():
        ref = identify(line.strip()) if line.strip() and not line.lstrip().startswith("#") else None
        if ref:
            out.setdefault(ref.source_id, ref)
    return list(out.values())


def add_urls(path: Path, urls: Iterable[str]) -> tuple[list[JobRef], list[str]]:
    """Append recognised job URLs to the queue file. Returns (added, rejected lines)."""
    existing = {r.source_id for r in queued_urls(path)}
    added, rejected = [], []
    for u in urls:
        u = u.strip()
        if not u or u.startswith("#"):
            continue
        ref = identify(u)
        if ref is None:
            rejected.append(u)
        elif ref.source_id not in existing:
            existing.add(ref.source_id)
            added.append(ref)
    if added:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            for r in added:
                f.write(r.url + "\n")
    return added, rejected


def saved_page_name(ref: JobRef) -> str:
    return f"{ref.site}_{ref.job_id}.html"


def short_hash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:10]


__all__ = ["identify", "unwrap", "site_of", "parse_job_page", "parse_alert_email", "JobAlertSource",
           "SavedPagesSource", "PageBlocked", "NEVER_AUTOMATE", "JOB_SITES", "add_urls", "queued_urls", "JobRef"]
