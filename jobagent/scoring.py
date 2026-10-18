"""Deterministic, explainable resume-to-job match rubric.

The score measures how well a specific resume document evidences the job's
requirements, weighted by the candidate's verified proficiency. It is this
tool's own rubric; it is not, and must not be presented as, any employer's
ATS score.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .config import DEFAULT_WEIGHTS
from .profile import DEGREE_LEVELS, PROFICIENCY_WEIGHT, Experience, Profile
from .requirements import JobRequirements, Requirement
from .resume_doc import ResumeDoc
from .skills import ADJACENT_FAMILIES, extract_skills, implied_closure, related, role_families
from .text import numbers_in, overlap, stems

DISCLAIMER = ("Match score from this tool's transparent rubric. It is not the score any employer's "
              "applicant tracking system will compute.")
DEMONSTRATED, LISTED_ONLY, TRANSFERABLE = 1.0, 0.6, 0.35


@dataclass
class Category:
    name: str
    weight: float
    score: float | None             # 0..1, None = not applicable
    evidence: list[dict] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class ScoreResult:
    total: float
    categories: dict[str, Category]
    missing_requirements: list[str]
    eligibility_failures: list[str]
    transferable_skills: list[dict]
    recommended_changes: list[str]
    confidence: str
    confidence_reasons: list[str]
    keyword_alignment: float
    disclaimer: str = DISCLAIMER

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["categories"] = {k: asdict(v) for k, v in self.categories.items()}
        return d


def skill_credit(skill: str, doc: ResumeDoc, profile: Profile, bullet_skills: dict, listed: set) -> tuple[float, str, Any]:
    """Credit for one required skill, how it was evidenced, and the evidence."""
    entry = profile.skill_entry(skill)
    prof = PROFICIENCY_WEIGHT[entry.proficiency] if entry else PROFICIENCY_WEIGHT["unknown"]
    if skill in bullet_skills:
        return DEMONSTRATED * prof, "demonstrated", bullet_skills[skill][:3]
    if skill in listed:
        return LISTED_ONLY * prof, "listed_only", "skills section"
    for rel in sorted(related(skill)):
        if rel in bullet_skills or rel in listed:
            return TRANSFERABLE, "transferable", rel
    return 0.0, "missing", None


def _bullet_similarity(req: Requirement, doc: ResumeDoc) -> tuple[float, str | None, str | None]:
    best, best_id, best_text = 0.0, None, None
    for b in doc.bullets():
        text_sim = overlap(req.text, b.text)
        if req.skills:
            bs = extract_skills(b.text)
            skill_sim = len(req.skills & implied_closure(bs)) / len(req.skills)
            sim = 0.6 * text_sim + 0.4 * skill_sim
        else:
            sim = text_sim
        if sim > best:
            best, best_id, best_text = sim, b.id, b.text
    return best, best_id, best_text


def _relevant_months(doc: ResumeDoc, job_title: str) -> int:
    job_fams = role_families(job_title)
    near = set(job_fams)
    for f in job_fams:
        near |= ADJACENT_FAMILIES.get(f, set())
    months = 0
    for r in doc.roles:
        fams = role_families(r.title)
        weight = 1.0 if (not job_fams or fams & near or "software" in fams) else 0.5
        months += int(Experience(id=r.exp_id, employer=r.employer, title=r.title, start=r.start, end=r.end).months() * weight)
    return months


def score(doc: ResumeDoc, profile: Profile, reqs: JobRequirements, job_title: str,
          weights: dict[str, float] | None = None, eligibility_failures: list[str] | None = None) -> ScoreResult:
    w = dict(DEFAULT_WEIGHTS)
    w.update(weights or {})
    bullet_skills = doc.bullet_skills()
    listed = doc.listed_skills()
    cats: dict[str, Category] = {}
    transferable: list[dict] = []
    recs: list[str] = []
    profile_demo = profile.demonstrated_skills()
    verified = profile.verified_skills()
    inferred_credit = 0

    # ---- required skills ---------------------------------------------------------------
    req_skills = reqs.skills("required") or reqs.skills("responsibility")
    c = Category("required_skills", w["required_skills"], None)
    if req_skills:
        credits = []
        for s in sorted(req_skills):
            val, how, ev = skill_credit(s, doc, profile, bullet_skills, listed)
            credits.append(val)
            c.evidence.append({"skill": s, "credit": round(val, 2), "how": how, "evidence": ev})
            if how == "missing":
                c.missing.append(s)
                if s in verified:
                    recs.append(f"Add verified evidence of {s} (profile items: {', '.join(profile_demo.get(s, ['skills list']))})")
                else:
                    recs.append(f"No verified evidence of {s}: leave it out; address it in the cover letter only if true")
            elif how == "listed_only":
                inferred_credit += 1
                if s in profile_demo:
                    recs.append(f"Show {s} in an experience bullet (evidence: {', '.join(profile_demo[s][:2])})")
            elif how == "transferable":
                inferred_credit += 1
                c.missing.append(s)  # still missing; partial credit only for the adjacent skill
                transferable.append({"required": s, "candidate_has": ev})
        c.score = sum(credits) / len(credits)
    cats[c.name] = c

    # ---- responsibilities ---------------------------------------------------------------
    c = Category("responsibilities", w["responsibilities"], None)
    resp = reqs.by_kind("responsibility")
    if resp:
        vals = []
        for r in resp:
            sim, bid, btext = _bullet_similarity(r, doc)
            credit = min(1.0, sim / 0.45)
            vals.append(credit)
            c.evidence.append({"requirement": r.text, "credit": round(credit, 2), "best_bullet": bid, "bullet": btext})
            if credit < 0.3:
                c.missing.append(r.text)
        c.score = sum(vals) / len(vals)
    cats[c.name] = c

    # ---- relevant experience and achievements ------------------------------------------------
    c = Category("experience", w["experience"], None)
    months = _relevant_months(doc, job_title)
    years = months / 12
    if reqs.min_years:
        yrs = min(1.0, years / reqs.min_years)
        c.notes.append(f"{years:.1f} relevant years vs {reqs.min_years}+ requested")
        if years < reqs.min_years:
            c.missing.append(f"{reqs.min_years}+ years of experience (resume shows {years:.1f})")
    else:
        yrs = 1.0 if years >= 1 else 0.5
        c.notes.append(f"{years:.1f} relevant years; no explicit requirement")
    jd_text = "\n".join(r.text for r in reqs.requirements)
    ranked = sorted(doc.bullets(), key=lambda b: overlap(b.text, jd_text), reverse=True)[:4]
    quantified = [b for b in ranked if numbers_in(b.text) and overlap(b.text, jd_text) > 0.1]
    ach = len(quantified) / max(1, min(3, len(ranked))) if ranked else 0.0
    ach = min(1.0, ach)
    c.evidence += [{"bullet": b.id, "text": b.text} for b in quantified]
    c.score = 0.65 * yrs + 0.35 * ach
    cats[c.name] = c

    # ---- education and certifications --------------------------------------------------------
    c = Category("education", w["education"], None)
    parts = []
    if reqs.degree_level:
        have = max((DEGREE_LEVELS.get((e.level or "").lower(), 0) for e in doc.education), default=0)
        if have >= reqs.degree_level:
            parts.append(1.0)
        elif have == reqs.degree_level - 1:
            parts.append(0.5 if reqs.degree_required else 0.8)
            c.missing.append("degree level below the stated requirement")
        else:
            parts.append(0.0 if reqs.degree_required else 0.5)
            c.missing.append("stated degree requirement")
        c.evidence += [{"education": f"{e.degree}, {e.institution}"} for e in doc.education]
    for cert in reqs.certifications:
        hit = next((x for x in doc.certifications if overlap(cert, x.name) >= 0.6), None)
        parts.append(1.0 if hit else 0.0)
        if hit:
            c.evidence.append({"certification": hit.name})
        else:
            c.missing.append(f"certification: {cert}")
    if parts:
        c.score = sum(parts) / len(parts)
    cats[c.name] = c

    # ---- domain -----------------------------------------------------------------------------
    c = Category("domain", w["domain"], None)
    if reqs.domains:
        have = doc.domains(profile)
        hit = reqs.domains & have
        c.score = len(hit) / len(reqs.domains)
        c.evidence.append({"job_domains": sorted(reqs.domains), "resume_domains": sorted(have)})
        c.missing += sorted(reqs.domains - have)
    cats[c.name] = c

    # ---- preferred qualifications ----------------------------------------------------------------
    c = Category("preferred", w["preferred"], None)
    pref = reqs.by_kind("preferred")
    if pref:
        vals = []
        for r in pref:
            if r.skills:
                sub = [skill_credit(s, doc, profile, bullet_skills, listed) for s in sorted(r.skills)]
                val = sum(x[0] for x in sub) / len(sub)
                for s, (v, how, ev) in zip(sorted(r.skills), sub):
                    if how == "transferable":
                        transferable.append({"preferred": s, "candidate_has": ev})
            else:
                sim, _, _ = _bullet_similarity(r, doc)
                val = min(1.0, sim / 0.45)
            vals.append(val)
            c.evidence.append({"requirement": r.text, "credit": round(val, 2)})
            if val < 0.3:
                c.missing.append(r.text)
        c.score = sum(vals) / len(vals)
    cats[c.name] = c

    applicable = [x for x in cats.values() if x.score is not None and x.weight > 0]
    wsum = sum(x.weight for x in applicable)
    total = round(100 * sum(x.weight * x.score for x in applicable) / wsum, 1) if wsum else 0.0

    # ---- confidence ---------------------------------------------------------------------------
    reasons = []
    n = len(reqs.requirements)
    conf = "high" if n >= 8 else "medium" if n >= 4 else "low"
    reasons.append(f"{n} requirements extracted from the job description")
    if len(applicable) < 4:
        conf = {"high": "medium", "medium": "low", "low": "low"}[conf]
        reasons.append(f"only {len(applicable)} of 6 rubric categories applicable")
    if req_skills and inferred_credit / max(1, len(req_skills)) > 0.4:
        conf = {"high": "medium", "medium": "low", "low": "low"}[conf]
        reasons.append("much of the skills credit is listed-only or transferable, not demonstrated")

    jd_skills = reqs.skills("required") | reqs.skills("preferred") | reqs.skills("responsibility")
    doc_mentions = extract_skills(doc.text())
    alignment = round(100 * len(jd_skills & doc_mentions) / len(jd_skills), 1) if jd_skills else 0.0

    missing = [f"skill: {s}" for s in cats["required_skills"].missing]
    for k in ("responsibilities", "experience", "education", "preferred"):
        missing += [f"{k}: {m}" for m in cats[k].missing]
    if cats["domain"].missing:
        missing.append("domain: " + ", ".join(cats["domain"].missing))

    relevant_unused = _unused_relevant_bullets(doc, profile, reqs)
    if relevant_unused:
        recs.append("Consider verified profile items not on this resume: " + ", ".join(relevant_unused[:5]))
    return ScoreResult(total=total, categories=cats, missing_requirements=missing,
                       eligibility_failures=list(eligibility_failures or []), transferable_skills=transferable,
                       recommended_changes=_dedup(recs), confidence=conf, confidence_reasons=reasons,
                       keyword_alignment=alignment)


def _unused_relevant_bullets(doc: ResumeDoc, profile: Profile, reqs: JobRequirements) -> list[str]:
    used = {b.id for b in doc.bullets()}
    jd = "\n".join(r.text for r in reqs.requirements)
    jd_stems = stems(jd)
    out = []
    for b in profile.all_bullets():
        if b.id not in used and len(stems(b.text) & jd_stems) >= 2:
            out.append(b.id)
    return out


def _dedup(items: list[str]) -> list[str]:
    seen, out = set(), []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out
