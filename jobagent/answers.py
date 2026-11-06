"""Screening-question answering from candidate-approved information only.

Every answer records where it came from. Questions the profile cannot
answer, sensitive questions without a pre-approved answer, and consequential
declarations are returned as needing the candidate, never guessed.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from .filtering import countries_in
from .profile import Profile

SENSITIVE = {
    "gender": r"\b(gender|sex)\b(?! offender)", "race_ethnicity": r"\b(race|ethnic)", "veteran_status": r"\bveteran",
    "disability_status": r"\bdisabilit", "sexual_orientation": r"sexual orientation|lgbt", "age": r"\b(date of birth|age|dob)\b",
    "criminal_record": r"criminal|convicted|felony", "religion": r"\breligio", "marital_status": r"marital",
    "pronouns": r"\bpronoun",
}
CONSEQUENTIAL = r"\b(i certify|i attest|i declare|i acknowledge|i agree|i consent|consent to|agree to|accept the|terms|" \
                r"privacy (policy|notice)|background check|non-?compete|signature|e-?sign|truthful)\b"

AUTH = (r"authori[sz]ed to work|legally (eligible|authori[sz]ed|permitted)|right to work|work permit|eligible to work|"
        r"permitted to work|(can|could) you (legally )?work|able to (legally )?work")
SPONSOR = r"sponsor|visa|\b(need|require)\w* (a |an )?(new )?work permit"
# "authorized to work ... without sponsorship": the polarity of an authorization question, not a sponsorship one
WITHOUT_SPONSOR = r"without (the )?(need for |needing |requiring |requirement of |any )?(an? )?(employer |company )?(visa|sponsor|work permit)"
NEEDS = r"\b(require|requires|requiring|need|needs|needing)\b"
YES_NO = {"yes", "no"}

RULES: list[tuple[str, str]] = [
    ("first_name", r"^first name|given name|^first\b"), ("last_name", r"^last name|surname|family name|^last\b"),
    ("full_name", r"^(full )?name\b|legal name|your name"), ("email", r"e-?mail"),
    ("phone", r"^(phone|mobile|cell|telephone)\b|\b(phone|mobile|cell|contact|telephone) (number|no\b)|your (phone|mobile)\b"), ("linkedin", r"linkedin"), ("github", r"github"),
    ("website", r"website|portfolio|personal site"), ("resume", r"\b(resume|cv|curriculum)\b"),
    ("cover_letter", r"cover letter"),
    ("work_authorization", AUTH), ("sponsorship", SPONSOR),
    ("relocation", r"relocat"),
    ("current_salary", r"(current|present|existing|last drawn) (ctc|salary|compensation|pay|package)"), ("salary", r"salary|(expected|desired|target) (compensation|pay|package)|compensation expectation|\bctc\b|pay expectation|remuneration"),
    ("notice_period", r"notice period|when can you (start|join)|earliest start|start date|availability to (start|join)|joining date|date of joining|available to join"),
    ("years_experience", r"years of (professional |relevant |work )?experience|how many years"),
    ("education", r"highest (level of )?(education|qualification)|highest degree|level of education"),
    ("current_employer", r"current (employer|company)"), ("current_title", r"current (title|role|position|designation)"),
    ("location", r"current location|where are you (located|based)|^city|^location"),
    ("how_did_you_hear", r"how did you (hear|find)|^source\b"), ("referral", r"referr"), ("hybrid", r"hybrid|in[- ]office|on-?site .*days"),
    ("motivation", r"why (do you want|are you interested|this company|us)|what interests you|tell us why"),
]


@dataclass
class Answer:
    question: str
    kind: str
    value: str | None
    source: str | None
    status: str          # answered | needs_input | sensitive_needs_input | consequential | generated_needs_review | file
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def classify(question: str) -> str:
    q = question.lower().strip()
    if re.search(CONSEQUENTIAL, q):
        return "consequential"
    for key, rx in SENSITIVE.items():
        if re.search(rx, q):
            return "sensitive:" + key
    auth, spon = re.search(AUTH, q), re.search(SPONSOR, q)
    if (auth or spon) and re.search(WITHOUT_SPONSOR, q):
        return "work_authorization_no_sponsorship"
    if auth and spon:
        return "work_authorization_ambiguous"
    for kind, rx in RULES:
        if re.search(rx, q):
            return kind
    return "unknown"


def _yes(v: bool) -> str:
    return "Yes" if v else "No"


def answer(question: str, profile: Profile, job: dict, options: list[str] | None = None) -> Answer:
    kind = classify(question)
    c = profile.contact
    pa = profile.approved_answers

    def ok(value, source, note=""):
        if value in (None, ""):
            return Answer(question, kind, None, None, "needs_input", f"profile has no {source}")
        if options and {o.strip().lower() for o in options} <= YES_NO and str(value).strip().lower() not in YES_NO:
            return Answer(question, kind, None, None, "needs_input",
                          f"yes/no question, but the profile answer for {source} is not yes or no")
        return Answer(question, kind, _match_option(str(value), options), source, "answered", note)

    if kind.startswith("sensitive:"):
        key = kind.split(":", 1)[1]
        if key in pa:
            return Answer(question, kind, _match_option(pa[key], options), f"approved_answers.{key}", "answered",
                          "candidate pre-approved this answer")
        return Answer(question, kind, None, None, "sensitive_needs_input", "sensitive question: candidate must answer")
    if kind == "consequential":
        key = "consent:" + re.sub(r"\W+", "_", question.lower()).strip("_")[:60]
        if pa.get(key):
            return Answer(question, kind, pa[key], f"approved_answers.{key}", "answered", "candidate pre-approved")
        return Answer(question, kind, None, None, "consequential", "declaration or consent: requires the candidate's review")
    if kind in ("first_name", "last_name", "full_name"):
        parts = c.name.split()
        return ok({"first_name": parts[0], "last_name": " ".join(parts[1:]) or None, "full_name": c.name}[kind], "contact.name")
    if kind in ("email", "phone", "linkedin", "github", "website"):
        return ok(getattr(c, kind), f"contact.{kind}")
    if kind == "location":
        return ok(c.location, "contact.location")
    if kind in ("resume", "cover_letter"):
        return Answer(question, kind, None, "documents", "file")
    if kind == "work_authorization_ambiguous":
        return Answer(question, kind, None, None, "needs_input",
                      "question mixes work authorization and sponsorship; cannot tell which answer it expects")
    if kind in ("work_authorization", "sponsorship", "work_authorization_no_sponsorship"):
        countries = countries_in(question) or countries_in(job.get("location"))
        if len(countries) != 1:
            return Answer(question, kind, None, None, "needs_input", "cannot tell which country the question is about")
        cc = next(iter(countries))
        auth = profile.work_authorization
        if kind == "work_authorization_no_sponsorship":
            # Yes only when authorized AND the candidate has not said they need sponsorship there
            if auth.requires_sponsorship.get(cc) is True:
                return ok("No", f"work_authorization.requires_sponsorship.{cc}", "candidate stated sponsorship need")
            if cc in auth.authorized and (cc in auth.requires_sponsorship
                                          or auth.authorized[cc].lower() in ("citizen", "permanent resident")):
                return ok("Yes", f"work_authorization.authorized.{cc}", "authorized; no sponsorship needed")
            return Answer(question, kind, None, None, "needs_input",
                          f"work authorization without sponsorship for {cc} not clear from profile")
        if kind == "work_authorization":
            if cc in auth.authorized:
                return ok("Yes", f"work_authorization.authorized.{cc}", auth.authorized[cc])
            if cc in auth.requires_sponsorship:
                return ok("No", f"work_authorization.requires_sponsorship.{cc}", "not authorized; candidate stated sponsorship need")
            return Answer(question, kind, None, None, "needs_input", f"work authorization for {cc} not in profile")
        if not re.search(NEEDS, question.lower()):
            return Answer(question, kind, None, None, "needs_input",
                          "visa/sponsorship question that does not ask whether sponsorship is needed")
        if cc in auth.requires_sponsorship:
            return ok(_yes(auth.requires_sponsorship[cc]), f"work_authorization.requires_sponsorship.{cc}")
        if cc in auth.authorized and auth.authorized[cc].lower() in ("citizen", "permanent resident"):
            return ok("No", f"work_authorization.authorized.{cc}")
        return Answer(question, kind, None, None, "needs_input", f"sponsorship need for {cc} not in profile")
    if kind == "relocation":
        v = profile.preferences.willing_to_relocate
        return ok(None if v is None else _yes(v), "preferences.willing_to_relocate")
    if kind == "current_salary":  # never answered with the expected salary
        if profile.salary.current_text:
            return ok(profile.salary.current_text, "salary.current_text")
        return Answer(question, kind, None, None, "needs_input", "current salary not in profile")
    if kind == "salary":
        s = profile.salary
        if s.answer_text:
            return ok(s.answer_text, "salary.answer_text")
        if s.expected is not None:
            if options or re.search(r"number|numeric|amount", question, re.I):
                return ok(f"{s.expected:.0f}", "salary.expected")
            return ok(f"{s.expected:,.0f} {s.currency or ''} per {s.interval}".strip(), "salary.expected")
        return Answer(question, kind, None, None, "needs_input", "salary expectation not in profile")
    if kind == "notice_period":
        if profile.availability_date:
            return ok(profile.availability_date, "availability_date")
        if profile.notice_period_days is not None:
            return ok(f"{profile.notice_period_days} days", "notice_period_days")
        return Answer(question, kind, None, None, "needs_input", "notice period not in profile")
    if kind == "years_experience":
        from .skills import canonical, extract_skills
        named = extract_skills(question)
        if len(named) > 1:
            return Answer(question, kind, None, None, "needs_input",
                          f"question names several skills ({', '.join(sorted(named))}); answer per skill")
        m = re.search(r"experience\b[\w ]{0,30}?\b(?:with|in|using) (?!total\b|all\b)([\w+#. /-]+)", question, re.I)
        if named or m:
            skill = next(iter(named)) if named else canonical(m.group(1).strip(" ?"))
            entry = profile.skill_entry(skill)
            if entry and entry.years is not None:
                return ok(f"{entry.years:g}", f"skills.{entry.name}.years")
            return Answer(question, kind, None, None, "needs_input", f"years with {skill} not in profile")
        years = profile.total_months() // 12
        return ok(str(years), "computed from experience dates")
    if kind == "education":
        if not profile.education:
            return Answer(question, kind, None, None, "needs_input", "no education in profile")
        top = max(profile.education, key=lambda e: {"phd": 5, "master": 4, "mba": 4, "bachelor": 3}.get((e.level or ""), 0))
        level = {"bachelor": "Bachelor's Degree", "master": "Master's Degree", "mba": "Master's Degree", "phd": "Doctorate"}.get(top.level or "", top.degree)
        return ok(level, f"education.{top.id}")
    if kind in ("current_employer", "current_title"):
        cur = next((e for e in profile.experience if e.end is None), None)
        return ok(None if not cur else (cur.employer if kind == "current_employer" else cur.title), "experience (current role)")
    if kind == "how_did_you_hear":
        return ok(pa.get("how_did_you_hear"), "approved_answers.how_did_you_hear")
    if kind == "hybrid":
        return ok(pa.get("willing_to_work_hybrid"), "approved_answers.willing_to_work_hybrid")
    if kind == "motivation":
        key = "motivation"
        if pa.get(key):
            return Answer(question, kind, pa[key], f"approved_answers.{key}", "generated_needs_review",
                          "generic approved answer; review for this employer")
        return Answer(question, kind, None, None, "needs_input", "motivation questions need the candidate's own words")
    if kind in pa:
        return ok(pa[kind], f"approved_answers.{kind}")
    # unknown: exact pre-approved text for this question?
    key = re.sub(r"\W+", "_", question.lower()).strip("_")[:60]
    if key in pa:
        return Answer(question, kind, pa[key], f"approved_answers.{key}", "answered")
    return Answer(question, kind, None, None, "needs_input", "question not covered by the profile")


def _match_option(value: str, options: list[str] | None) -> str:
    if not options:
        return value
    v = value.strip().lower()
    for o in options:
        if o.strip().lower() == v:
            return o
    for o in options:
        if o.strip().lower().startswith(v) or v.startswith(o.strip().lower()):
            return o
    return value
