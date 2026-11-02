"""Deterministic job-description parser: JD text -> typed requirements and eligibility constraints.

Job descriptions are untrusted input. This module only extracts data from
them; nothing in a JD can change the candidate's policy or the scoring rules.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .skills import domains_in, extract_skills

HEADINGS = {
    "responsibility": ["responsibilities", "what you'll do", "what you will do", "the role", "your role", "role overview",
                       "day to day", "day-to-day", "in this role", "your impact", "what you'll be doing", "key responsibilities",
                       "job responsibilities", "duties", "the job", "you will"],
    "required": ["requirements", "qualifications", "minimum qualifications", "basic qualifications", "what you'll need",
                 "what you will need", "what we're looking for", "what we are looking for", "must have", "must-have",
                 "must haves", "you have", "who you are", "required skills", "required qualifications", "skills",
                 "about you", "what you bring", "you should have", "job requirements", "skills & experience",
                 "skills and experience", "experience"],
    "preferred": ["preferred", "preferred qualifications", "nice to have", "nice-to-have", "nice to haves", "bonus",
                  "bonus points", "good to have", "pluses", "plus", "additional qualifications", "desired",
                  "desired qualifications", "it would be great if"],
    "ignore": ["about us", "about the company", "who we are", "benefits", "perks", "what we offer", "why join us",
               "equal opportunity", "eeo", "compensation", "salary", "our values", "location", "how to apply", "about the team"],
}
PREFERRED_CUES = re.compile(r"\b(preferred|nice to have|nice-to-have|is a plus|a plus|bonus|good to have|desirable|"
                            r"ideally|would be great|advantageous)\b", re.I)
REQUIRED_CUES = re.compile(r"\b(must|required|requirement|need to have|minimum|at least|mandatory|essential)\b", re.I)
RESP_VERBS = re.compile(r"^(you will |you'll )?(design|build|develop|own|lead|drive|implement|maintain|collaborate|"
                        r"work with|partner|create|deliver|manage|write|ship|scale|improve|mentor|define|operate|"
                        r"support|analy[sz]e|architect|deploy|monitor|participate|contribute|help)\b", re.I)
YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:-|–|to)?\s*(\d{1,2})?\s*\+?\s*(?:years|yrs)", re.I)
DEGREE_REQ = [
    ("phd", re.compile(r"\b(ph\.?\s?d|doctorate)\b", re.I)),
    ("master", re.compile(r"\b(master'?s|m\.?\s?s\.?\b|m\.?\s?tech|mba)\b", re.I)),
    ("bachelor", re.compile(r"\b(bachelor'?s|b\.?\s?s\.?\b|b\.?\s?tech|b\.?\s?e\.?\b|undergraduate degree|degree in)\b", re.I)),
]
DEGREE_LEVEL = {"bachelor": 3, "master": 4, "phd": 5}
CERT_PATTERNS = re.compile(r"\b(aws certified[\w\s–-]*?(?=[,.;)]|$)|cka|ckad|pmp|cissp|cpa|cfa|"
                           r"google cloud certified[\w\s-]*|azure certified[\w\s-]*|scrum master certification|csm)\b", re.I)
COUNTRIES = {
    "US": r"u\.?s\.?a?\.?|united states|america", "GB": r"u\.?k\.?|united kingdom|britain", "IN": r"india",
    "CA": r"canada", "DE": r"germany", "EU": r"\beu\b|european union|eea", "AU": r"australia", "SG": r"singapore",
    "IE": r"ireland", "NL": r"netherlands",
}


@dataclass
class Requirement:
    id: str
    text: str
    kind: str  # required | preferred | responsibility
    skills: set[str] = field(default_factory=set)
    min_years: int | None = None


@dataclass
class Eligibility:
    work_auth_countries: list[str] = field(default_factory=list)   # must be authorized to work here
    no_sponsorship: bool = False
    sponsorship_available: bool = False
    citizenship_required: list[str] = field(default_factory=list)
    clearance_required: str | None = None
    location_required: str | None = None
    languages_required: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class JobRequirements:
    requirements: list[Requirement]
    eligibility: Eligibility
    domains: set[str]
    min_years: int | None
    degree_level: int
    degree_required: bool
    certifications: list[str]

    def by_kind(self, kind: str) -> list[Requirement]:
        return [r for r in self.requirements if r.kind == kind]

    def skills(self, kind: str) -> set[str]:
        out: set[str] = set()
        for r in self.by_kind(kind):
            out |= r.skills
        return out


def _heading_kind(line: str) -> str | None:
    if line.strip()[:1] in "-•*●▪◦·" and not line.strip().startswith("**"):
        return None
    s = line.strip().rstrip(":").strip().lower()
    s = re.sub(r"^[#*\s]+|[*\s]+$", "", s)
    if len(s) > 60 or not s:
        return None
    if not (line.strip().endswith(":") or len(s.split()) <= 6):
        return None
    for kind, names in HEADINGS.items():
        for n in names:
            if s == n or (line.strip().endswith(":") and (s.startswith(n) or s.endswith(" " + n))):
                return kind
    return None


def _items(lines: list[str]) -> list[str]:
    out: list[str] = []
    for ln in lines:
        t = ln.strip()
        if not t:
            continue
        if t[:1] in "-•*●▪◦·" or re.match(r"^\d+[.)]\s", t):
            out.append(re.sub(r"^([-•*●▪◦·]|\d+[.)])\s*", "", t))
        elif out and t[:1].islower():
            out[-1] += " " + t
        else:
            # paragraph: split into sentences
            out.extend(s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", t) if len(s.strip()) > 3)
    return out


def _classify_free(sentence: str) -> str | None:
    if PREFERRED_CUES.search(sentence):
        return "preferred"
    if REQUIRED_CUES.search(sentence) or YEARS.search(sentence):
        return "required"
    if RESP_VERBS.search(sentence):
        return "responsibility"
    if extract_skills(sentence):
        return "required"
    return None


def parse_eligibility(text: str) -> Eligibility:
    e = Eligibility()
    t = text.lower()
    for code, rx in COUNTRIES.items():
        if re.search(rf"(authori[sz]ed|eligible|right) to work (in|within) (the )?({rx})", t):
            e.work_auth_countries.append(code)
        if re.search(rf"({rx}) citizen(ship)? (is )?(required|only)|must be an? ({rx}) citizen|({rx}) citizens only", t):
            e.citizenship_required.append(code)
    if re.search(r"(no|not able to|unable to|cannot|can't|will not|won't|do not|don't|does not) (provide |offer )?"
                 r"(visa )?sponsor|sponsorship (is )?not (available|provided|offered)|without (the need for )?(visa )?sponsorship", t):
        e.no_sponsorship = True
    elif re.search(r"(visa )?sponsorship (is )?(available|provided|offered)|we (can|will) sponsor", t):
        e.sponsorship_available = True
    m = re.search(r"\b(ts/sci|top secret|secret clearance|security clearance|sc clearance|dv clearance)\b", t)
    if m and not re.search(r"no (security )?clearance (is )?required", t):
        e.clearance_required = m.group(1)
    m = re.search(r"must (be (located|based) in|reside in|live in|be within commuting distance of) ([a-z ,.]+?)(?:[.;\n]|$)", t)
    if m:
        e.location_required = m.group(4).strip(" ,.")
    for m in re.finditer(r"(?:fluent|fluency|native|proficient|proficiency) in (english|german|french|spanish|japanese|"
                         r"mandarin|dutch|hindi|portuguese|italian|korean|arabic)", t):
        if m.group(1).title() not in e.languages_required:
            e.languages_required.append(m.group(1).title())
    return e


def parse_job(description: str, required_hint: list[str] | None = None,
              preferred_hint: list[str] | None = None) -> JobRequirements:
    sections: dict[str, list[str]] = {"responsibility": [], "required": [], "preferred": [], "ignore": [], "intro": []}
    current = "intro"
    for line in (description or "").splitlines():
        kind = _heading_kind(line)
        if kind:
            current = kind
            continue
        sections[current].append(line)

    raw: list[tuple[str, str]] = []
    structured = any(sections[k] for k in ("responsibility", "required", "preferred"))
    for kind in ("responsibility", "required", "preferred"):
        for item in _items(sections[kind]):
            k = kind
            if kind == "required" and PREFERRED_CUES.search(item):
                k = "preferred"
            raw.append((k, item))
    if not structured:
        for item in _items(sections["intro"]):
            k = _classify_free(item)
            if k:
                raw.append((k, item))
    for item in required_hint or []:
        raw.append(("preferred" if PREFERRED_CUES.search(item) else "required", item))
    for item in preferred_hint or []:
        raw.append(("preferred", item))

    seen: set[str] = set()
    reqs: list[Requirement] = []
    for kind, text in raw:
        key = re.sub(r"\W+", " ", text.lower()).strip()
        if key in seen or len(key) < 2:
            continue
        seen.add(key)
        years = None
        ym = YEARS.search(text)
        if ym and kind != "responsibility":
            years = int(ym.group(1))
        reqs.append(Requirement(id=f"r{len(reqs) + 1}", text=text, kind=kind, skills=extract_skills(text), min_years=years))

    relevant_text = "\n".join(r.text for r in reqs) or description
    # A line's level is the lowest degree it names ("Bachelor's or Master's" is satisfied by a bachelor's).
    # A required line sets the bar; a preferred degree ("Master's a plus") never raises it.
    degree_level, degree_required = 0, False
    by_kind: dict[str, list[tuple[int, Requirement]]] = {"required": [], "preferred": []}
    for r in reqs:
        levels = [DEGREE_LEVEL[name] for name, rx in DEGREE_REQ if rx.search(r.text)]
        if levels and r.kind in by_kind:
            by_kind[r.kind].append((min(levels), r))
    if by_kind["required"]:
        degree_level, line = max(by_kind["required"], key=lambda x: x[0])
        degree_required = not re.search(r"or equivalent (practical )?experience", line.text, re.I)
    elif by_kind["preferred"]:
        degree_level = max(lv for lv, _ in by_kind["preferred"])
    req_years = [r.min_years for r in reqs if r.min_years is not None and r.kind == "required" and r.min_years <= 25]
    certs = sorted({m.group(1).strip() for m in CERT_PATTERNS.finditer(relevant_text)})
    return JobRequirements(
        requirements=reqs, eligibility=parse_eligibility(description or ""),
        domains=domains_in(description or ""), min_years=max(req_years) if req_years else None,
        degree_level=degree_level, degree_required=degree_required, certifications=certs,
    )
