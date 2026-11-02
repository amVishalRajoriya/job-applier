"""Candidate profile: the single source of truth for every factual claim.

Fields the candidate has not filled in stay ``None``. Nothing downstream may
infer them; unanswered fields become questions for the candidate.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from . import security
from .db import Database, now
from .skills import canonical, extract_skills, implied_closure

Proficiency = Literal["expert", "advanced", "intermediate", "beginner", "exposure", "unknown"]
PROFICIENCY_WEIGHT = {"expert": 1.0, "advanced": 1.0, "intermediate": 0.85, "beginner": 0.6, "exposure": 0.4, "unknown": 0.8}
PROFICIENCY_ORDER = ["exposure", "beginner", "intermediate", "advanced", "expert"]
DEGREE_LEVELS = {"highschool": 1, "diploma": 2, "associate": 2, "bachelor": 3, "master": 4, "mba": 4, "phd": 5}


class Bullet(BaseModel):
    id: str
    text: str
    on_master_resume: bool = True

    @property
    def skills(self) -> set[str]:
        return extract_skills(self.text)


class Experience(BaseModel):
    id: str
    employer: str
    title: str
    start: str  # YYYY-MM
    end: Optional[str] = None  # None = present
    location: Optional[str] = None
    domains: list[str] = Field(default_factory=list)
    bullets: list[Bullet] = Field(default_factory=list)
    on_master_resume: bool = True

    def months(self, today: date | None = None) -> int:
        today = today or date.today()
        sy, sm = (int(x) for x in self.start.split("-")[:2])
        if self.end:
            ey, em = (int(x) for x in self.end.split("-")[:2])
        else:
            ey, em = today.year, today.month
        return max(0, (ey - sy) * 12 + (em - sm) + 1)


class Education(BaseModel):
    id: str
    institution: str
    degree: str
    level: Optional[str] = None
    field: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    coursework: list[str] = Field(default_factory=list)
    on_master_resume: bool = True


class Certification(BaseModel):
    id: str
    name: str
    issuer: Optional[str] = None
    date: Optional[str] = None
    on_master_resume: bool = True


class Project(BaseModel):
    id: str
    name: str
    description: str = ""
    bullets: list[Bullet] = Field(default_factory=list)
    url: Optional[str] = None
    on_master_resume: bool = True


class SkillEntry(BaseModel):
    name: str
    proficiency: Proficiency = "unknown"
    years: Optional[float] = None
    on_master_resume: bool = True

    @field_validator("name")
    @classmethod
    def _canon(cls, v: str) -> str:
        return canonical(v)


class Contact(BaseModel):
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    linkedin: Optional[str] = None
    github: Optional[str] = None
    website: Optional[str] = None


class WorkAuthorization(BaseModel):
    # country code -> status text the candidate approved (e.g. "citizen", "permanent resident", "H-1B")
    authorized: dict[str, str] = Field(default_factory=dict)
    # country code -> needs sponsorship? None/missing = unknown, ask the candidate
    requires_sponsorship: dict[str, bool] = Field(default_factory=dict)


class Salary(BaseModel):
    currency: Optional[str] = None
    minimum: Optional[float] = None
    expected: Optional[float] = None
    interval: str = "year"
    answer_text: Optional[str] = None  # candidate-approved wording, e.g. "Negotiable, 30-35 LPA"
    current_text: Optional[str] = None  # candidate-approved answer to "current CTC/salary" (Naukri asks), e.g. "24 LPA"


class Preferences(BaseModel):
    target_roles: list[str] = Field(default_factory=list)
    adjacent_roles: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    remote: list[Literal["remote", "hybrid", "onsite"]] = Field(default_factory=lambda: ["remote", "hybrid", "onsite"])
    willing_to_relocate: Optional[bool] = None
    employment_types: list[str] = Field(default_factory=lambda: ["full-time"])
    excluded_employers: list[str] = Field(default_factory=list)
    required_keywords_any: list[str] = Field(default_factory=list)
    excluded_keywords: list[str] = Field(default_factory=list)
    security_clearance: Optional[str] = None
    languages: list[str] = Field(default_factory=list)


class Profile(BaseModel):
    synthetic: bool = False
    contact: Contact
    summary: Optional[str] = None
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    achievements: list[Bullet] = Field(default_factory=list)
    skills: list[SkillEntry] = Field(default_factory=list)
    preferences: Preferences = Field(default_factory=Preferences)
    work_authorization: WorkAuthorization = Field(default_factory=WorkAuthorization)
    salary: Salary = Field(default_factory=Salary)
    notice_period_days: Optional[int] = None
    availability_date: Optional[str] = None
    approved_answers: dict[str, str] = Field(default_factory=dict)
    documents: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique_ids(self) -> "Profile":
        ids = [b.id for b in self.all_bullets()] + [e.id for e in self.experience] + [p.id for p in self.projects]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ValueError(f"duplicate ids in profile: {sorted(dup)}")
        return self

    # ---- evidence helpers ----------------------------------------------
    def all_bullets(self) -> list[Bullet]:
        out: list[Bullet] = []
        for e in self.experience:
            out.extend(e.bullets)
        for p in self.projects:
            out.extend(p.bullets)
        out.extend(self.achievements)
        return out

    def bullet(self, bullet_id: str) -> Bullet | None:
        for b in self.all_bullets():
            if b.id == bullet_id:
                return b
        return None

    def skill_entry(self, name: str) -> SkillEntry | None:
        for s in self.skills:
            if s.name.lower() == name.lower():
                return s
        return None

    def demonstrated_skills(self) -> dict[str, list[str]]:
        """skill -> bullet ids where the skill is explicitly used (plus implied skills)."""
        out: dict[str, list[str]] = {}
        for b in self.all_bullets():
            for s in implied_closure(b.skills):
                out.setdefault(s, []).append(b.id)
        for p in self.projects:
            for s in implied_closure(extract_skills(p.description)):
                out.setdefault(s, []).append(p.id)
        return out

    def verified_skills(self) -> set[str]:
        """Every skill the profile supports: listed skills plus skills demonstrated in evidence."""
        return implied_closure({s.name for s in self.skills}) | set(self.demonstrated_skills())

    def total_months(self) -> int:
        # merge overlapping ranges so concurrent roles are not double counted
        spans = []
        for e in self.experience:
            sy, sm = (int(x) for x in e.start.split("-")[:2])
            start = sy * 12 + sm
            spans.append((start, start + e.months() - 1))
        spans.sort()
        total, cur_s, cur_e = 0, None, None
        for s, e in spans:
            if cur_s is None:
                cur_s, cur_e = s, e
            elif s <= cur_e + 1:
                cur_e = max(cur_e, e)
            else:
                total += cur_e - cur_s + 1
                cur_s, cur_e = s, e
        if cur_s is not None:
            total += cur_e - cur_s + 1
        return total

    def highest_degree_level(self) -> int:
        return max((DEGREE_LEVELS.get((e.level or "").lower(), 0) for e in self.education), default=0)

    def missing_personal_fields(self) -> list[str]:
        """Fields the candidate must supply before applications can be completed."""
        missing = []
        if not self.contact.email:
            missing.append("contact.email")
        if not self.contact.phone:
            missing.append("contact.phone")
        if not self.work_authorization.authorized:
            missing.append("work_authorization.authorized")
        if not self.work_authorization.requires_sponsorship:
            missing.append("work_authorization.requires_sponsorship")
        if self.salary.expected is None and not self.salary.answer_text:
            missing.append("salary.expected")
        if self.notice_period_days is None:
            missing.append("notice_period_days")
        if self.preferences.willing_to_relocate is None:
            missing.append("preferences.willing_to_relocate")
        return missing

    def fingerprint(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()[:16]


# ---- persistence ----------------------------------------------------------------

def load_yaml(path: str) -> Profile:
    with open(path) as f:
        return Profile.model_validate(yaml.safe_load(f))


def save(db: Database, profile: Profile, actor: str = "candidate") -> None:
    raw = profile.model_dump_json().encode()
    blob, encrypted = security.encrypt(raw)
    with db.tx() as c:
        c.execute("INSERT INTO profile(id, blob, encrypted, updated_at) VALUES (1,?,?,?) "
                  "ON CONFLICT(id) DO UPDATE SET blob=excluded.blob, encrypted=excluded.encrypted, updated_at=excluded.updated_at",
                  (blob, int(encrypted), now()))
    db.audit(actor, "profile_saved", "profile", 1, fingerprint=profile.fingerprint(), encrypted=encrypted,
             synthetic=profile.synthetic)


def load(db: Database) -> Profile:
    row = db.one("SELECT blob, encrypted FROM profile WHERE id=1")
    if row is None:
        raise LookupError("no candidate profile yet: run `jobagent profile import <profile.yaml>`")
    data = security.decrypt(row["blob"], bool(row["encrypted"]))
    return Profile.model_validate(json.loads(data))


def to_yaml(profile: Profile) -> str:
    return yaml.safe_dump(json.loads(profile.model_dump_json()), sort_keys=False, allow_unicode=True)
