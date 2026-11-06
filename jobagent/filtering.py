"""Eligibility, relevance, expiry and fraud screening.

Hard disqualifiers are limited to genuine, candidate-configured constraints.
Unknowns become warnings that require candidate input, never silent rejections.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

from .normalize import CITY_ALIASES, employer_matches
from .profile import Profile
from .requirements import JobRequirements
from .security import detect_injection
from .skills import ADJACENT_FAMILIES, extract_skills, role_families
from .text import overlap

COUNTRY_HINTS = {
    "IN": ["india", "bengaluru", "bangalore", "mumbai", "pune", "hyderabad", "chennai", "delhi", "gurugram", "gurgaon",
           "noida", "kolkata", "ahmedabad", "kochi", "jaipur", "chandigarh"],
    "US": ["united states", "usa", "u.s.", " us", "us-", "us)", "new york", "san francisco", "seattle", "austin", "boston",
           "chicago", "los angeles", "denver", "atlanta", "california", "texas", "washington, dc", ", ny", ", ca", ", wa", ", tx", ", ma"],
    "GB": ["united kingdom", " uk", "uk)", "london", "manchester", "edinburgh", "england", "scotland"],
    "CA": ["canada", "toronto", "vancouver", "montreal"],
    "DE": ["germany", "berlin", "munich", "hamburg"],
    "SG": ["singapore"], "AU": ["australia", "sydney", "melbourne"], "IE": ["ireland", "dublin"],
    "NL": ["netherlands", "amsterdam"], "AE": ["dubai", "united arab emirates", "uae", "abu dhabi"],
}
FRAUD_PATTERNS = [
    (r"(pay|deposit|send)\s+(a |an )?(registration|training|processing|application|onboarding)?\s*fee", 2),
    (r"(registration|training|security|processing) (fee|deposit)", 2),
    (r"(wire|western union|moneygram|gift ?cards?|bitcoin|crypto(currency)?)\s+(transfer|payment|to)", 2),
    (r"interview (via|on|over) (telegram|whatsapp|signal|google hangouts)", 2),
    (r"(contact|message) (us|me|the recruiter) (on|via) (telegram|whatsapp)", 2),
    (r"(provide|send) (your )?(bank (account )?details|ssn|social security number|aadhaar|pan card) (before|to apply|with your application)", 3),
    (r"(earn|make) \$?\d[\d,]* (per|a|every) (day|week) (from home|working from home)", 2),
    (r"no (experience|interview) (needed|required).{0,40}(earn|\$)", 1),
    (r"(@gmail\.com|@yahoo\.com|@outlook\.com|@hotmail\.com)", 1),
    (r"(guaranteed|instant) (job|hiring|placement)", 1),
]


@dataclass
class ScreenResult:
    passed: bool
    relevance: float
    disqualifiers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)  # material facts the candidate must supply
    fraud_signals: list[str] = field(default_factory=list)
    injection: list[str] = field(default_factory=list)
    expired: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def countries_in(text: str | None) -> set[str]:
    t = " " + (text or "").lower() + " "
    return {code for code, hints in COUNTRY_HINTS.items() if any(h in t for h in hints)}


def _city(text: str) -> str:
    first = re.split(r"[,;/|(-]", text.lower())[0].strip()
    return CITY_ALIASES.get(first, first)


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s[:10]).date()
    except ValueError:
        return None


def relevance(job_title: str, description: str, reqs: JobRequirements, profile: Profile) -> float:
    prefs = profile.preferences
    target_fams = set().union(*(role_families(r) for r in prefs.target_roles)) if prefs.target_roles else set()
    adjacent_fams = set().union(*(role_families(r) for r in prefs.adjacent_roles)) if prefs.adjacent_roles else set()
    for f in list(target_fams):
        adjacent_fams |= ADJACENT_FAMILIES.get(f, set())
    job_fams = role_families(job_title)
    title_score = 0.0
    if any(overlap(job_title, r) >= 0.6 for r in prefs.target_roles):
        title_score = 1.0
    elif job_fams & target_fams:
        title_score = 1.0
    elif any(overlap(job_title, r) >= 0.6 for r in prefs.adjacent_roles) or job_fams & adjacent_fams:
        title_score = 0.7
    jd_skills = reqs.skills("required") | reqs.skills("preferred") | reqs.skills("responsibility") or extract_skills(description)
    skill_score = len(jd_skills & profile.verified_skills()) / len(jd_skills) if jd_skills else 0.0
    return round(0.6 * title_score + 0.4 * skill_score, 3)


LIVE_LISTING_SOURCES = {"greenhouse", "lever", "ashby", "workday"}


_PER_YEAR = {"hour": 2080, "day": 260, "week": 52, "month": 12, "year": 1}
_INTERVAL_ALIASES = {"hourly": "hour", "daily": "day", "weekly": "week", "monthly": "month", "yearly": "year",
                     "annual": "year", "annually": "year", "annum": "year", "per annum": "year", "p.a.": "year"}


def _interval(v: str | None) -> str | None:
    v = (v or "").strip().lower().replace("per-", "").replace("per ", "") or None
    if v is None:
        return None
    v = _INTERVAL_ALIASES.get(v, v)
    v = _INTERVAL_ALIASES.get(v.split("-")[0], v.split("-")[0])  # Lever: "year-salary", "hour-wage"
    return v if v in _PER_YEAR else ("?" + v)


def _to_interval(amount: float, frm: str | None, to: str | None) -> float | None:
    """Convert a pay amount between intervals. None if either interval is not recognised.
    An unstated job interval is taken to be the candidate's (salaries in postings are usually annual)."""
    f, t = _interval(frm), _interval(to) or "year"
    if f is None:
        return amount
    if f.startswith("?") or t.startswith("?"):
        return None
    return amount * _PER_YEAR[f] / _PER_YEAR[t]


def _from_text(job: dict, key: str) -> bool:
    return bool(job.get(key) or (job.get("raw") or {}).get(key))


def screen(job: dict, reqs: JobRequirements, profile: Profile, *, relevance_threshold: float = 0.25,
           max_age_days: int = 60, today: date | None = None) -> ScreenResult:
    today = today or date.today()
    prefs = profile.preferences
    auth = profile.work_authorization
    r = ScreenResult(passed=True, relevance=0.0)
    title, desc = job.get("title") or "", job.get("description") or ""
    loc = job.get("location") or ""
    remote = job.get("remote_policy")

    # ---- employer, expiry, fraud, injection ------------------------------------
    if employer_matches(job.get("employer"), prefs.excluded_employers):
        r.disqualifiers.append("employer is on the candidate's exclusion list")
    closing = _parse_date(job.get("closing_date"))
    posted = _parse_date(job.get("date_posted"))
    if closing and closing < today:
        r.expired = True
        r.disqualifiers.append(f"closing date {closing} has passed")
    elif posted and posted < today - timedelta(days=max_age_days):
        if set(job.get("sources") or [job.get("source")]) & LIVE_LISTING_SOURCES:
            # It came from the employer's live ATS listing, so it is still open; many employers keep evergreen
            # roles posted for years. Flag the age but do not close it.
            r.warnings.append(f"first posted {posted}, but still listed on the employer's board")
        else:
            r.expired = True
            r.disqualifiers.append(f"posted {posted}, older than {max_age_days} days")
    score = 0
    for rx, weight in FRAUD_PATTERNS:
        m = re.search(rx, desc, re.I)
        if m:
            r.fraud_signals.append(m.group(0))
            score += weight
    if score >= 2:
        r.disqualifiers.append("likely fraudulent listing: " + "; ".join(r.fraud_signals))
    r.injection = detect_injection(desc)
    if r.injection:
        r.warnings.append("job description contains instruction-like text aimed at AI tools; treated as data only")

    # ---- employment type ---------------------------------------------------------
    et = job.get("employment_type")
    if et and prefs.employment_types and et not in prefs.employment_types:
        msg = f"employment type {et} not in preferences {prefs.employment_types}"
        if _from_text(job, "employment_type_from_text"):
            r.warnings.append(msg + " (guessed from the description text; check the posting)")
        else:
            r.disqualifiers.append(msg)

    # ---- location / remote --------------------------------------------------------
    pref_remote_ok = "remote" in prefs.remote
    pref_cities = {_city(p) for p in prefs.locations if "remote" not in p.lower()}
    if remote == "remote":
        if not pref_remote_ok:
            r.disqualifiers.append("remote role but candidate does not want remote work")
    elif remote in ("hybrid", "onsite") or loc:
        policy = remote or "onsite"
        if policy not in prefs.remote and remote is not None:
            r.disqualifiers.append(f"{policy} role but candidate accepts {prefs.remote}")
        elif loc and pref_cities:
            job_cities = {_city(x) for x in re.split(r";|\bor\b", loc) if x.strip()}
            if not job_cities & pref_cities:
                if prefs.willing_to_relocate is True:
                    r.warnings.append(f"location {loc} requires relocation (candidate is willing)")
                elif prefs.willing_to_relocate is False:
                    r.disqualifiers.append(f"location {loc} is outside preferred locations and candidate will not relocate")
                else:
                    r.unknowns.append(f"location {loc} is outside preferred locations; is the candidate willing to relocate?")
    if not loc and remote is None:
        r.warnings.append("location and remote policy not stated")

    # ---- work authorization -----------------------------------------------------------
    el = reqs.eligibility
    countries = set(el.work_auth_countries) | countries_in(loc)
    remote_countries = countries_in(loc) if remote == "remote" else set()
    for c in sorted(countries):
        if c in auth.authorized:
            continue
        needs = auth.requires_sponsorship.get(c)
        if needs is None:
            r.unknowns.append(f"work authorization for {c} is not in the profile")
        elif needs and el.no_sponsorship:
            r.disqualifiers.append(f"requires work authorization in {c}; job states no sponsorship and candidate needs it")
        elif needs and c in remote_countries:
            r.disqualifiers.append(f"remote role restricted to {c}, where the candidate is not authorized to work")
        elif needs and not el.sponsorship_available:
            r.warnings.append(f"candidate needs sponsorship for {c}; job does not say whether it sponsors")
    for c in el.citizenship_required:
        if (auth.authorized.get(c) or "").lower() != "citizen":
            r.disqualifiers.append(f"{c} citizenship required")
    if el.clearance_required and not prefs.security_clearance:
        r.disqualifiers.append(f"requires {el.clearance_required}")
    for lang in el.languages_required:
        if prefs.languages and lang not in prefs.languages:
            r.disqualifiers.append(f"requires fluency in {lang}")
        elif not prefs.languages:
            r.unknowns.append(f"job requires {lang}; candidate languages not in profile")
    if el.location_required:
        req_city = _city(el.location_required)
        if pref_cities and req_city not in pref_cities and not countries_in(el.location_required) & set(auth.authorized):
            r.warnings.append(f"job requires being located in {el.location_required}")

    # ---- salary -----------------------------------------------------------------------
    smax, cur = job.get("salary_max"), job.get("salary_currency")
    if smax and cur and profile.salary.minimum and profile.salary.currency == cur:
        annual = _to_interval(smax, job.get("salary_interval"), profile.salary.interval)
        if annual is None:
            if smax < profile.salary.minimum:
                r.warnings.append(f"salary max {smax:,.0f} {cur} per {job.get('salary_interval')} could not be "
                                  f"compared with the candidate minimum per {profile.salary.interval}")
        elif annual < profile.salary.minimum:
            msg = f"salary max {smax:,.0f} {cur} below candidate minimum {profile.salary.minimum:,.0f}"
            if annual != smax:
                msg = (f"salary max {smax:,.0f} {cur} per {job.get('salary_interval')} (about {annual:,.0f} per "
                       f"{profile.salary.interval}) below candidate minimum {profile.salary.minimum:,.0f}")
            if _from_text(job, "salary_from_text"):
                r.warnings.append(msg + " (read from the description text; check the posting)")
            else:
                r.disqualifiers.append(msg)

    # ---- relevance and seniority ------------------------------------------------------
    r.relevance = relevance(title, desc, reqs, profile)
    if any(k.lower() in title.lower() for k in prefs.excluded_keywords):
        r.disqualifiers.append("title contains an excluded keyword")
    if r.relevance < relevance_threshold:
        r.disqualifiers.append(f"not relevant to target roles (relevance {r.relevance:.2f})")
    if reqs.min_years and reqs.min_years > profile.total_months() / 12 + 4:
        r.warnings.append(f"asks for {reqs.min_years}+ years; candidate has {profile.total_months() / 12:.1f}")

    r.passed = not r.disqualifiers
    return r
