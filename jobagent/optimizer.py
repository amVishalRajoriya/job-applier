"""Evidence-based resume tailoring.

Only verified profile evidence is ever added. Changes are chosen greedily by
how much they improve the rubric, every change is recorded with its reason
and evidence ids, and the result must pass the truth guard. If the target
range cannot be reached honestly, the honest score is kept and reported.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Callable

from .profile import Profile
from .requirements import JobRequirements
from .resume_doc import RBullet, RCert, RProject, RRole, ResumeDoc
from .scoring import ScoreResult, score
from .skills import IMPLIES, SKILLS, extract_skills, implied_closure
from .text import overlap, stems
from .truth_guard import check_free_text, check_rewrite, validate_resume


class TruthViolation(Exception):
    def __init__(self, violations):
        super().__init__("; ".join(str(v) for v in violations))
        self.violations = violations


@dataclass
class Change:
    kind: str
    target: str
    reason: str
    evidence: list[str]
    before: str | None = None
    after: str | None = None
    score_delta: float | None = None


@dataclass
class OptimizationResult:
    baseline: ScoreResult
    final: ScoreResult
    doc: ResumeDoc
    changes: list[Change] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    outcome: str = "not_required"   # not_required | target_met | below_target_honest
    remaining_gaps: list[str] = field(default_factory=list)

    def report(self) -> dict:
        return {
            "baseline_score": self.baseline.total, "final_score": self.final.total, "outcome": self.outcome,
            "changes": [asdict(c) for c in self.changes], "rejected_suggestions": self.rejected,
            "remaining_gaps": self.remaining_gaps, "disclaimer": self.final.disclaimer,
        }


@dataclass
class _Candidate:
    change: Change
    apply: Callable[[ResumeDoc], None]


def _jd_text(reqs: JobRequirements) -> str:
    return "\n".join(r.text for r in reqs.requirements)


def _relevance(text: str, reqs: JobRequirements, jd_skills: set[str]) -> float:
    jd = _jd_text(reqs)
    skill_hit = len(implied_closure(extract_skills(text)) & jd_skills)
    return overlap(text, jd) + 0.25 * skill_hit + 0.1 * len(stems(text) & stems(jd))


def _candidates(profile: Profile, doc: ResumeDoc, reqs: JobRequirements) -> list[_Candidate]:
    jd_skills = reqs.skills("required") | reqs.skills("preferred") | reqs.skills("responsibility")
    jd = _jd_text(reqs)
    out: list[_Candidate] = []
    in_doc = {b.id for b in doc.bullets()}
    roles = {r.exp_id for r in doc.roles}

    for e in profile.experience:
        relevant = [b for b in e.bullets if b.id not in in_doc and _relevance(b.text, reqs, jd_skills) >= 0.35]
        if e.id not in roles:
            if not relevant:
                continue
            def add_role(d: ResumeDoc, e=e, bs=relevant):
                d.roles.append(RRole(e.id, e.employer, e.title, e.start, e.end, e.location, [RBullet(b.id, b.text) for b in bs]))
                d.roles.sort(key=lambda r: r.start, reverse=True)
            out.append(_Candidate(Change("add_role", e.id, "verified role omitted from the master resume is relevant",
                                         [b.id for b in relevant], after=f"{e.title}, {e.employer}"), add_role))
            continue
        for b in relevant:
            def add_bullet(d: ResumeDoc, e=e, b=b):
                role = next(r for r in d.roles if r.exp_id == e.id)
                role.bullets.append(RBullet(b.id, b.text))
            out.append(_Candidate(Change("add_bullet", e.id, "verified achievement not on the master resume matches the job",
                                         [b.id], after=b.text), add_bullet))

    doc_projects = {p.proj_id for p in doc.projects}
    for p in profile.projects:
        relevant = [b for b in p.bullets if b.id not in in_doc and _relevance(b.text, reqs, jd_skills) >= 0.35]
        if not relevant:
            continue
        if p.id not in doc_projects:
            def add_project(d: ResumeDoc, p=p, bs=relevant):
                d.projects.append(RProject(p.id, p.name, [RBullet(b.id, b.text) for b in bs]))
            out.append(_Candidate(Change("add_project", p.id, "relevant verified project", [b.id for b in relevant],
                                         after=p.name), add_project))
        else:
            for b in relevant:
                def add_pbullet(d: ResumeDoc, p=p, b=b):
                    next(x for x in d.projects if x.proj_id == p.id).bullets.append(RBullet(b.id, b.text))
                out.append(_Candidate(Change("add_bullet", p.id, "relevant verified project detail", [b.id], after=b.text),
                                      add_pbullet))

    doc_skills = set(doc.skills)
    for s in profile.skills:
        if s.name in doc_skills:
            continue
        if s.name in jd_skills or implied_closure({s.name}) & jd_skills:
            label = {"beginner": "beginner", "exposure": "basic exposure"}.get(s.proficiency, "")
            def add_skill(d: ResumeDoc, s=s, label=label):
                d.skills.append(s.name)
                if label:
                    d.skill_labels[s.name] = label
            out.append(_Candidate(Change("add_skill", "skills", f"verified skill ({s.proficiency}) requested by the job",
                                         [f"skill:{s.name}"], after=s.name + (f" ({label})" if label else "")), add_skill))

    for e in profile.education:
        doc_edu = next((x for x in doc.education if x.edu_id == e.id), None)
        if not doc_edu:
            continue
        for course in e.coursework:
            if course not in doc_edu.coursework and (overlap(course, jd) >= 0.5 or extract_skills(course) & jd_skills):
                def add_course(d: ResumeDoc, eid=e.id, course=course):
                    next(x for x in d.education if x.edu_id == eid).coursework.append(course)
                out.append(_Candidate(Change("add_coursework", e.id, "relevant completed coursework", [f"{e.id}:{course}"],
                                             after=course), add_course))

    doc_certs = {c.cert_id for c in doc.certifications}
    for c in profile.certifications:
        if c.id not in doc_certs and (overlap(c.name, jd) >= 0.4 or extract_skills(c.name) & jd_skills):
            def add_cert(d: ResumeDoc, c=c):
                d.certifications.append(RCert(c.id, c.name, c.issuer, c.date))
            out.append(_Candidate(Change("add_certification", c.id, "relevant verified certification", [c.id], after=c.name),
                                  add_cert))
    return out


def _alias_pattern(skill: str) -> re.Pattern:
    names = [skill] + SKILLS.get(skill, [])
    return re.compile(r"(?<![\w.+#/-])(" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")(?![\w+#-])", re.I)


_ABSTRACT = {"Observability", "Data Visualization", "ETL", "Deep Learning"}


def _terminology(doc: ResumeDoc, profile: Profile, reqs: JobRequirements) -> list[Change]:
    """Name a requested skill next to the specific tool that implies it, e.g. 'Django (Python)'."""
    changes = []
    jd_skills = reqs.skills("required") | reqs.skills("preferred") | reqs.skills("responsibility")
    mentioned = extract_skills(doc.text())
    for target in sorted(jd_skills - mentioned - _ABSTRACT):
        implying = [s for s, implied in IMPLIES.items() if target in implied]
        for b in doc.bullets():
            src_skills = extract_skills(b.text)
            hit = next((s for s in implying if s in src_skills), None)
            if not hit:
                continue
            m = _alias_pattern(hit).search(b.text)
            if not m:
                continue
            new = b.text[: m.end()] + f" ({target})" + b.text[m.end():]
            if check_rewrite(profile.bullet(b.id).text, new):
                continue
            changes.append(Change("terminology", b.id, f"{hit} implies {target}, which the job asks for",
                                  [b.id], before=b.text, after=new))
            b.text = new
            break
    return changes


def _reorder(doc: ResumeDoc, reqs: JobRequirements) -> list[Change]:
    jd_skills = reqs.skills("required") | reqs.skills("preferred") | reqs.skills("responsibility")
    changes = []
    for r in doc.roles + doc.projects:
        before = [b.id for b in r.bullets]
        r.bullets.sort(key=lambda b: _relevance(b.text, reqs, jd_skills), reverse=True)
        after = [b.id for b in r.bullets]
        if before != after:
            target = getattr(r, "exp_id", None) or getattr(r, "proj_id")
            changes.append(Change("reorder_bullets", target, "most relevant evidence first", after,
                                  before=", ".join(before), after=", ".join(after)))
    before = list(doc.skills)
    doc.skills.sort(key=lambda s: (s not in jd_skills, not (implied_closure({s}) & jd_skills), before.index(s)))
    if before != doc.skills:
        changes.append(Change("reorder_skills", "skills", "requested skills first", [], before=", ".join(before),
                              after=", ".join(doc.skills)))
    return changes


def _prune(doc: ResumeDoc, reqs: JobRequirements, max_bullets: int = 5) -> list[Change]:
    jd_skills = reqs.skills("required") | reqs.skills("preferred") | reqs.skills("responsibility")
    changes = []
    for r in doc.roles:
        while len(r.bullets) > max_bullets:
            worst = min(r.bullets, key=lambda b: _relevance(b.text, reqs, jd_skills))
            if _relevance(worst.text, reqs, jd_skills) > 0.3:
                break
            r.bullets.remove(worst)
            changes.append(Change("remove_bullet", worst.id, "least relevant line removed to keep the resume focused",
                                  [worst.id], before=worst.text))
    return changes


def tailored_summary(profile: Profile, doc: ResumeDoc, reqs: JobRequirements) -> str | None:
    if not doc.roles:
        return profile.summary
    years = profile.total_months() // 12
    title = doc.roles[0].title
    jd_skills = reqs.skills("required") | reqs.skills("preferred") | reqs.skills("responsibility")
    demonstrated = doc.bullet_skills()
    solid = {x.name for x in profile.skills if x.proficiency in ("expert", "advanced", "intermediate")}
    top = [s for s in demonstrated if s in jd_skills and s in solid]  # beginner/exposure skills stay out of the headline
    top = sorted(top, key=lambda s: -len(demonstrated[s]))[:5]
    domains = sorted(set(reqs.domains) & doc.domains(profile))
    parts = [f"{title} with {years} years of experience"]
    if domains:
        parts.append(" in " + " and ".join(domains))
    if top:
        parts.append(", working with " + (", ".join(top[:-1]) + " and " + top[-1] if len(top) > 1 else top[0]))
    return "".join(parts) + "."


def optimize(profile: Profile, master: ResumeDoc, reqs: JobRequirements, job_title: str, *,
             weights: dict | None = None, optimize_below: float = 50.0, target_min: float = 70.0,
             target_max: float = 80.0, eligibility_failures: list[str] | None = None, llm=None) -> OptimizationResult:
    def rescore(d: ResumeDoc) -> ScoreResult:
        return score(d, profile, reqs, job_title, weights, eligibility_failures)

    baseline = rescore(master)
    doc = master.copy()
    changes: list[Change] = []
    rejected: list[dict] = []
    current = baseline.total
    needs_optimization = baseline.total < optimize_below

    if needs_optimization:
        pool = _candidates(profile, doc, reqs)
        while pool and current < target_max:
            best, best_score = None, current
            for cand in pool:
                trial = doc.copy()
                cand.apply(trial)
                s = rescore(trial).total
                if s > best_score + 0.05:
                    best, best_score = cand, s
            if best is None:
                break
            best.apply(doc)
            best.change.score_delta = round(best_score - current, 1)
            changes.append(best.change)
            pool.remove(best)
            current = best_score
        changes += _terminology(doc, profile, reqs)
        summary = tailored_summary(profile, doc, reqs)
        problems = check_free_text(summary or "", profile)
        if summary and not problems:
            changes.append(Change("summary", "summary", "summary restated from verified facts relevant to the job",
                                  ["profile"], before=doc.summary, after=summary))
            doc.summary = summary
        elif problems:
            rejected.append({"kind": "summary", "text": summary, "problems": problems})
        if llm is not None:
            from .llm import polish_bullets
            accepted, rej = polish_bullets(llm, profile, doc, reqs)
            changes += [Change("llm_rewrite", a["id"], "clearer wording (model suggestion, passed truth guard)",
                               [a["id"]], before=a["before"], after=a["after"]) for a in accepted]
            rejected += rej
        changes += _prune(doc, reqs)
    changes += _reorder(doc, reqs)

    violations = validate_resume(doc, profile)
    if violations:
        raise TruthViolation(violations)
    final = rescore(doc)
    if not needs_optimization:
        outcome = "not_required"
    elif final.total >= target_min:
        outcome = "target_met"
    else:
        outcome = "below_target_honest"
    gaps = [g for g in final.missing_requirements]
    return OptimizationResult(baseline, final, doc, changes, rejected, outcome, gaps)
