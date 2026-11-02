"""Truth guard: every claim in a tailored resume or letter must trace to verified profile evidence.

The guard is deterministic and runs on all generated text, whether produced
by templates or by the language model. A violation blocks the document.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .profile import Profile
from .resume_doc import ResumeDoc
from .skills import SKILLS, extract_skills, implied_closure
from .text import numbers_in

OWNERSHIP = re.compile(r"\b(led|lead|leading|owned|own|owning|architected|spearheaded|managed|headed|directed|founded|"
                       r"drove|launched|pioneered|oversaw|supervised|built and led|single-handedly|sole)\b", re.I)
SENIORITY = re.compile(r"\b(expert|world-class|guru|ninja|rockstar|mastery|deep expertise|extensive experience)\b", re.I)
ALLOWED_CAPS = {"I", "API", "APIs", "UI", "UX", "CI", "CD", "QA", "IT", "SaaS", "B2B", "B2C", "KPI", "KPIs", "SLA", "SLAs",
                "ETL", "ML", "AI", "SQL", "REST", "HTTP", "JSON", "OKR", "OKRs", "MVP", "PR", "PRs", "ECS", "EKS"}


@dataclass
class Violation:
    where: str
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.where}: {self.rule}: {self.detail}"


def _caps_entities(text: str) -> set[str]:
    """Capitalised words that are not sentence-initial: likely names of employers, products, places."""
    out = set()
    for sent in re.split(r"(?<=[.!?;:])\s+|\n", text):
        words = re.findall(r"[A-Za-z][\w+#.-]*", sent)
        for w in words[1:]:
            w = w.rstrip(".")
            if w[:1].isupper() and w not in ALLOWED_CAPS:
                out.add(w.lower())
    return out


def _skill_vocab() -> set[str]:
    vocab = set()
    for canon, aliases in SKILLS.items():
        vocab |= {x.lower() for x in canon.split()} | {a.lower() for al in aliases for a in al.split()}
    return vocab


_VOCAB = _skill_vocab()


def check_rewrite(source: str, new: str, extra_allowed_text: str = "") -> list[str]:
    """Rules for a rewritten line against its source line. Returns problems (empty = OK)."""
    problems = []
    src_nums = numbers_in(source)
    for n in numbers_in(new) - src_nums:
        problems.append(f"new number or metric '{n}' not in source")
    allowed_skills = implied_closure(extract_skills(source))
    for s in extract_skills(new) - allowed_skills:
        problems.append(f"skill '{s}' not evidenced by the source line")
    src_own = {m.lower() for m in OWNERSHIP.findall(source)}
    for m in OWNERSHIP.findall(new):
        if m.lower() not in src_own:
            problems.append(f"ownership/leadership claim '{m}' not in source")
    for m in SENIORITY.findall(new):
        if m.lower() not in source.lower():
            problems.append(f"proficiency inflation '{m}'")
    src_words = {w.lower().rstrip(".") for w in re.findall(r"[A-Za-z][\w+#.-]*", source + " " + extra_allowed_text)}
    for ent in _caps_entities(new):
        if ent not in src_words and ent not in _VOCAB:
            problems.append(f"new named entity '{ent}' not in source")
    return problems


def validate_resume(doc: ResumeDoc, profile: Profile) -> list[Violation]:
    v: list[Violation] = []
    c = profile.contact
    for key in ("name", "email", "phone"):
        if doc.contact.get(key) and doc.contact.get(key) != getattr(c, key):
            v.append(Violation("contact", "changed_fact", f"{key} differs from profile"))
    exps = {e.id: e for e in profile.experience}
    for r in doc.roles:
        e = exps.get(r.exp_id)
        if not e:
            v.append(Violation(r.exp_id, "unknown_role", "role not in profile"))
            continue
        for f in ("employer", "title", "start", "end"):
            if getattr(r, f) != getattr(e, f):
                v.append(Violation(r.exp_id, "changed_fact", f"{f} '{getattr(r, f)}' != profile '{getattr(e, f)}'"))
        own = {b.id: b for b in e.bullets}
        for b in r.bullets:
            src = own.get(b.id)
            if not src:
                v.append(Violation(b.id, "untraceable_bullet", "bullet is not from this role's verified evidence"))
                continue
            for p in check_rewrite(src.text, b.text):
                v.append(Violation(b.id, "unsupported_claim", p))
    projs = {p.id: p for p in profile.projects}
    for pr in doc.projects:
        p = projs.get(pr.proj_id)
        if not p or pr.name != p.name:
            v.append(Violation(pr.proj_id, "unknown_project", "project not in profile"))
            continue
        own = {b.id: b for b in p.bullets}
        for b in pr.bullets:
            src = own.get(b.id)
            if not src:
                v.append(Violation(b.id, "untraceable_bullet", "bullet is not from this project"))
                continue
            for prob in check_rewrite(src.text, b.text, p.description):
                v.append(Violation(b.id, "unsupported_claim", prob))
    profile_skills = {s.name: s for s in profile.skills}
    for s in doc.skills:
        if s not in profile_skills:
            v.append(Violation("skills", "unverified_skill", s))
        else:
            label = doc.skill_labels.get(s, "")
            prof = profile_skills[s].proficiency
            expected = {"beginner": "beginner", "exposure": "basic exposure"}.get(prof, "")
            if label != expected:
                v.append(Violation("skills", "proficiency_changed", f"{s}: '{label}' but profile says {prof}"))
    edus = {e.id: e for e in profile.education}
    for ed in doc.education:
        e = edus.get(ed.edu_id)
        if not e or (ed.institution, ed.degree, ed.end) != (e.institution, e.degree, e.end):
            v.append(Violation(ed.edu_id, "changed_fact", "education differs from profile"))
            continue
        for course in ed.coursework:
            if course not in e.coursework:
                v.append(Violation(ed.edu_id, "unverified_coursework", course))
    certs = {x.id: x for x in profile.certifications}
    for ct in doc.certifications:
        x = certs.get(ct.cert_id)
        if not x or ct.name != x.name:
            v.append(Violation(ct.cert_id, "unverified_certification", ct.name))
    if doc.summary and doc.summary != profile.summary:
        v += [Violation("summary", "unsupported_claim", p) for p in check_free_text(doc.summary, profile)]
    return v


def profile_text(profile: Profile) -> str:
    parts = [profile.summary or "", profile.contact.name]
    for e in profile.experience:
        parts += [e.employer, e.title, e.location or "", " ".join(e.domains)] + [b.text for b in e.bullets]
    for p in profile.projects:
        parts += [p.name, p.description] + [b.text for b in p.bullets]
    parts += [b.text for b in profile.achievements]
    parts += [f"{e.degree} {e.institution} {e.field or ''} {' '.join(e.coursework)}" for e in profile.education]
    parts += [f"{c.name} {c.issuer or ''}" for c in profile.certifications]
    parts += [s.name for s in profile.skills]
    return "\n".join(parts)


def check_free_text(text: str, profile: Profile, extra_numbers: set[str] | None = None,
                    extra_allowed_text: str = "", quoted: tuple[str, ...] = ()) -> list[str]:
    """Rules for free text (summary, cover letter): every number, skill and named entity must exist in the profile.

    `quoted` holds exact phrases that only name something, such as the job title and employer in
    "I am applying for the Lead Security Engineer role at Razorpay". They are removed before checking,
    because naming the role is not a claim about the candidate.
    """
    for phrase in sorted((q for q in quoted if q), key=len, reverse=True):
        text = re.sub(re.escape(phrase), " ", text, flags=re.I)
    ptext = profile_text(profile)
    years = profile.total_months() // 12
    allowed_nums = numbers_in(ptext) | {str(years), str(years + 1)} | (extra_numbers or set())
    problems = []
    for n in numbers_in(text) - allowed_nums:
        problems.append(f"number '{n}' not supported by the profile")
    domain_words = extract_skills(" ".join(d for e in profile.experience for d in e.domains))
    for s in extract_skills(text) - profile.verified_skills() - domain_words:
        problems.append(f"skill '{s}' not in the verified profile")
    for m in SENIORITY.findall(text):
        if m.lower() not in ptext.lower():
            problems.append(f"proficiency inflation '{m}'")
    p_own = {m.lower() for m in OWNERSHIP.findall(ptext)}
    for m in OWNERSHIP.findall(text):
        if m.lower() not in p_own:
            problems.append(f"ownership/leadership claim '{m}' not in the profile")
    words = {w.lower().rstrip(".") for w in re.findall(r"[A-Za-z][\w+#.-]*", ptext + " " + extra_allowed_text)}
    for ent in _caps_entities(text):
        if ent not in words and ent not in _VOCAB:
            problems.append(f"named entity '{ent}' not in the profile")
    return problems
