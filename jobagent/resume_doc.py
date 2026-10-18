"""Structured resume document. Every line carries the id of the profile evidence it came from."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass, field

from .profile import Profile
from .skills import domains_in, extract_skills, implied_closure


@dataclass
class RBullet:
    id: str        # profile bullet id this line is derived from
    text: str


@dataclass
class RRole:
    exp_id: str
    employer: str
    title: str
    start: str
    end: str | None
    location: str | None
    bullets: list[RBullet] = field(default_factory=list)


@dataclass
class RProject:
    proj_id: str
    name: str
    bullets: list[RBullet] = field(default_factory=list)


@dataclass
class REducation:
    edu_id: str
    institution: str
    degree: str
    level: str | None
    end: str | None
    coursework: list[str] = field(default_factory=list)


@dataclass
class RCert:
    cert_id: str
    name: str
    issuer: str | None
    date: str | None


@dataclass
class ResumeDoc:
    contact: dict
    summary: str | None
    roles: list[RRole]
    projects: list[RProject]
    skills: list[str]                 # entries rendered in the skills section
    skill_labels: dict[str, str]      # skill -> proficiency qualifier shown (e.g. "basic exposure")
    education: list[REducation]
    certifications: list[RCert]

    # ---- views ------------------------------------------------------------
    def bullets(self) -> list[RBullet]:
        out = [b for r in self.roles for b in r.bullets]
        out += [b for p in self.projects for b in p.bullets]
        return out

    def text(self) -> str:
        parts = [self.summary or ""]
        for r in self.roles:
            parts.append(f"{r.title} {r.employer}")
            parts += [b.text for b in r.bullets]
        for p in self.projects:
            parts.append(p.name)
            parts += [b.text for b in p.bullets]
        parts.append(", ".join(self.skills))
        for e in self.education:
            parts.append(f"{e.degree} {e.institution} {' '.join(e.coursework)}")
        parts += [c.name for c in self.certifications]
        return "\n".join(parts)

    def bullet_skills(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for b in self.bullets():
            for s in implied_closure(extract_skills(b.text)):
                out.setdefault(s, []).append(b.id)
        return out

    def listed_skills(self) -> set[str]:
        return implied_closure(set(self.skills))

    def domains(self, profile: Profile) -> set[str]:
        ids = {r.exp_id for r in self.roles}
        doms = {d for e in profile.experience if e.id in ids for d in e.domains}
        return doms | domains_in(self.text())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ResumeDoc":
        return cls(
            contact=d["contact"], summary=d.get("summary"),
            roles=[RRole(**{**r, "bullets": [RBullet(**b) for b in r["bullets"]]}) for r in d["roles"]],
            projects=[RProject(**{**p, "bullets": [RBullet(**b) for b in p["bullets"]]}) for p in d["projects"]],
            skills=list(d["skills"]), skill_labels=dict(d.get("skill_labels", {})),
            education=[REducation(**e) for e in d["education"]],
            certifications=[RCert(**c) for c in d["certifications"]],
        )

    def copy(self) -> "ResumeDoc":
        return copy.deepcopy(self)

    def sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True).encode()).hexdigest()


def master_resume(profile: Profile) -> ResumeDoc:
    """The candidate's master resume, built only from items marked on_master_resume."""
    c = profile.contact
    return ResumeDoc(
        contact={k: v for k, v in c.model_dump().items() if v},
        summary=profile.summary,
        roles=[RRole(e.id, e.employer, e.title, e.start, e.end, e.location,
                     [RBullet(b.id, b.text) for b in e.bullets if b.on_master_resume])
               for e in profile.experience if e.on_master_resume],
        projects=[RProject(p.id, p.name, [RBullet(b.id, b.text) for b in p.bullets if b.on_master_resume])
                  for p in profile.projects if p.on_master_resume],
        skills=[s.name for s in profile.skills if s.on_master_resume],
        skill_labels={s.name: _label(s.proficiency) for s in profile.skills if s.on_master_resume and _label(s.proficiency)},
        education=[REducation(e.id, e.institution, e.degree, e.level, e.end, []) for e in profile.education if e.on_master_resume],
        certifications=[RCert(c.id, c.name, c.issuer, c.date) for c in profile.certifications if c.on_master_resume],
    )


def _label(prof: str) -> str:
    return {"beginner": "beginner", "exposure": "basic exposure"}.get(prof, "")
