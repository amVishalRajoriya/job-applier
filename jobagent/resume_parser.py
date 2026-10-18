"""Master resume ingestion: PDF / DOCX / TXT / MD -> structured draft profile.

Parsing resumes is inherently heuristic, so the output is a *draft* the
candidate reviews (`profile.draft.yaml`). Only text that appears in the
resume is carried over; personal fields that a resume does not state
(work authorization, salary, notice period...) are left empty.
"""
from __future__ import annotations

import re
from pathlib import Path

from .profile import (Bullet, Certification, Contact, Education, Experience, Profile, Project,
                      SkillEntry)
from .security import clean_text
from .skills import canonical, domains_in, extract_skills

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_MON = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE = rf"(?:{_MON}\s+\d{{4}}|\d{{1,2}}/\d{{4}}|\d{{4}}-\d{{2}}|\d{{4}})"
DATE_RANGE = re.compile(rf"({_DATE})\s*(?:-|–|—|to)\s*({_DATE}|present|current|now|till date)", re.I)
SINGLE_DATE = re.compile(_DATE, re.I)
BULLET_CHARS = "•●▪◦‣∙*-–·"

SECTION_ALIASES = {
    "summary": ["summary", "professional summary", "profile", "about me", "objective", "career objective"],
    "experience": ["experience", "work experience", "professional experience", "employment history", "employment", "work history"],
    "education": ["education", "academic background", "academics", "education and training"],
    "skills": ["skills", "technical skills", "core skills", "key skills", "core competencies", "technologies", "skills & tools"],
    "projects": ["projects", "personal projects", "key projects", "selected projects"],
    "certifications": ["certifications", "certificates", "licenses & certifications", "licenses and certifications"],
    "achievements": ["achievements", "awards", "honors", "awards & achievements", "accomplishments"],
}
TITLE_WORDS = re.compile(r"\b(engineer|developer|manager|analyst|scientist|lead|intern|consultant|designer|architect|"
                         r"director|specialist|associate|officer|administrator|programmer|head|vp|president|"
                         r"sde|sre|devops|founder|researcher|coordinator|executive|trainee)\b", re.I)
DEGREE = re.compile(r"\b(b\.?\s?tech|b\.?\s?e\b|bachelor|b\.?\s?s\.?c?\b|b\.?a\b|m\.?\s?tech|m\.?\s?e\b|master|m\.?\s?s\.?c?\b|"
                    r"mba|ph\.?\s?d|doctorate|diploma|associate|high school|hsc|ssc|m\.?c\.?a|b\.?c\.?a)", re.I)


class ResumeParseError(Exception):
    pass


def extract_text(path: str | Path) -> str:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    elif suffix == ".docx":
        import docx
        d = docx.Document(str(path))
        parts = [("- " + p.text) if "list" in (p.style.name or "").lower() else p.text for p in d.paragraphs]
        for table in d.tables:
            for row in table.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        text = "\n".join(parts)
    elif suffix in (".txt", ".md", ".markdown"):
        text = path.read_text(encoding="utf-8", errors="replace")
        text = re.sub(r"^#+\s*", "", text, flags=re.M).replace("**", "")
    else:
        raise ResumeParseError(f"unsupported resume format {suffix}; use PDF, DOCX, TXT or MD")
    text = clean_text(text)
    if len(text) < 200:
        raise ResumeParseError("could not extract meaningful text (scanned PDF? export a text PDF or DOCX)")
    return text


def _section_of(line: str) -> str | None:
    key = re.sub(r"[^a-z& ]", "", line.lower()).strip()
    if len(key) > 40:
        return None
    for name, aliases in SECTION_ALIASES.items():
        if key in aliases:
            return name
    return None


def split_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {"header": []}
    current = "header"
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        name = _section_of(s)
        if name:
            current = name
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(s)
    return sections


def _ym(token: str, end: bool = False) -> str | None:
    t = token.strip().lower().rstrip(".")
    if t in ("present", "current", "now", "till date"):
        return None
    m = re.match(rf"({_MON})\s+(\d{{4}})", t)
    if m:
        return f"{m.group(2)}-{MONTHS[m.group(1)[:3]]:02d}"
    m = re.match(r"(\d{1,2})/(\d{4})", t)
    if m:
        return f"{m.group(2)}-{int(m.group(1)):02d}"
    m = re.match(r"(\d{4})-(\d{2})", t)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = re.match(r"(\d{4})", t)
    if m:
        return f"{m.group(1)}-{12 if end else 1:02d}"
    return None


def _is_bullet(line: str) -> bool:
    return line[:1] in BULLET_CHARS


def _strip_bullet(line: str) -> str:
    return line.lstrip(BULLET_CHARS + " ").strip()


def parse_contact(header: list[str], full_text: str) -> Contact:
    name = header[0] if header else "Unknown"
    if "@" in name or re.search(r"\d{5}", name):
        name = "Unknown"
    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", full_text)
    phone = re.search(r"(\+?\d[\d\s().-]{8,}\d)", full_text)
    linkedin = re.search(r"(https?://)?(www\.)?linkedin\.com/in/[\w-]+/?", full_text, re.I)
    github = re.search(r"(https?://)?(www\.)?github\.com/[\w-]+/?", full_text, re.I)
    location = None
    for line in header[1:4]:
        for part in re.split(r"[|•·]", line):
            part = part.strip()
            if re.fullmatch(r"[A-Z][A-Za-z .]+,\s*[A-Z][A-Za-z .]+", part):
                location = part
                break
        if location:
            break
    return Contact(
        name=name.strip(),
        email=email.group(0) if email else None,
        phone=re.sub(r"\s+", " ", phone.group(1)).strip() if phone else None,
        linkedin=("https://" + linkedin.group(0).split("://")[-1]) if linkedin else None,
        github=("https://" + github.group(0).split("://")[-1]) if github else None,
        location=location,
    )


def _split_title_employer(text: str) -> tuple[str, str, str | None]:
    text = text.strip(" ,|-–—")
    parts = [p.strip() for p in re.split(r"\s+(?:—|–|-|\||@|at)\s+|\s*\|\s*|,\s+", text) if p.strip()]
    if not parts:
        return "", "", None
    title_idx = next((i for i, p in enumerate(parts) if TITLE_WORDS.search(p)), None)
    if title_idx is None:
        title_idx = 0
    title = parts[title_idx]
    rest = [p for i, p in enumerate(parts) if i != title_idx]
    employer = rest[0] if rest else ""
    location = ", ".join(rest[1:]) or None
    return title, employer, location


def _looks_like_bullet(line: str) -> bool:
    """Bullet text whose glyph was lost in PDF/DOCX extraction: a sentence, not a header."""
    return line.endswith(".") or len(line) > 75 or (len(line.split()) > 6 and not TITLE_WORDS.search(line))


def parse_experience(lines: list[str]) -> list[Experience]:
    entries: list[dict] = []
    pending_header: list[str] = []
    for idx, raw in enumerate(lines):
        dr = DATE_RANGE.search(raw)
        if dr and not _is_bullet(raw):
            header_text = (raw[: dr.start()] + " " + raw[dr.end():]).strip(" ,|()-–—")
            header_lines = [h for h in pending_header + [header_text] if h]
            entries.append({"header": header_lines, "start": _ym(dr.group(1)), "end": _ym(dr.group(2), end=True),
                            "bullets": [], "header_done": bool(header_text and len(header_lines) >= 1 and "|" in raw)})
            pending_header = []
            continue
        if not entries:
            pending_header = (pending_header + [raw])[-2:]
            continue
        cur = entries[-1]
        if _is_bullet(raw):
            cur["bullets"].append(_strip_bullet(raw))
            cur["header_done"] = True
            continue
        if cur["bullets"] and raw[:1].islower():
            cur["bullets"][-1] += " " + raw  # wrapped continuation line
            continue
        upcoming_date = any(DATE_RANGE.search(x) for x in lines[idx + 1: idx + 3])
        if not upcoming_date or _looks_like_bullet(raw):
            if not cur["header_done"] and not cur["bullets"] and not _looks_like_bullet(raw) and len(raw) < 60:
                cur["header"].append(raw)  # e.g. "Employer, City" on the line under the dated title
                cur["header_done"] = True
            else:
                cur["bullets"].append(raw)
                cur["header_done"] = True
            continue
        pending_header = (pending_header + [raw])[-2:]

    out: list[Experience] = []
    for i, e in enumerate(entries, start=1):
        joined = " | ".join(e["header"])
        title, employer, location = _split_title_employer(joined)
        if not e["start"]:
            continue
        exp_id = f"exp{i}"
        bullets = [Bullet(id=f"{exp_id}.b{j}", text=b) for j, b in enumerate(e["bullets"], start=1) if len(b) > 3]
        out.append(Experience(id=exp_id, employer=employer or "UNKNOWN EMPLOYER", title=title or "UNKNOWN TITLE",
                              start=e["start"], end=e["end"], location=location,
                              domains=sorted(domains_in(" ".join(e["bullets"]) + " " + joined)), bullets=bullets))
    return out


def parse_education(lines: list[str]) -> list[Education]:
    out: list[Education] = []
    block: list[str] = []
    blocks: list[list[str]] = []
    for ln in lines:
        if DEGREE.search(ln) and block and any(DEGREE.search(b) for b in block):
            blocks.append(block)
            block = []
        block.append(_strip_bullet(ln))
    if block:
        blocks.append(block)
    for i, b in enumerate(blocks, start=1):
        text = " | ".join(b)
        deg = next((x for x in b if DEGREE.search(x)), None)
        if not deg:
            continue
        inst = next((x for x in b if re.search(r"universit|institute|college|school|iit|nit|bits", x, re.I) and x != deg), None)
        if inst is None and re.search(r"universit|institute|college", deg, re.I):
            parts = re.split(r",|\||—|–", deg)
            inst = next((p.strip() for p in parts if re.search(r"universit|institute|college", p, re.I)), None)
        dr = DATE_RANGE.search(text)
        dates = SINGLE_DATE.findall(text)
        level = _degree_level(deg)
        coursework = []
        cw = re.search(r"coursework\s*:\s*(.+)", text, re.I)
        if cw:
            coursework = [c.strip() for c in re.split(r",|;", cw.group(1).split("|")[0]) if c.strip()]
        degree_text = re.split(r"\s*\|\s*|\s+[—–]\s+|,\s+(?=[^,]*(?:universit|institute|college))", DATE_RANGE.sub("", deg), flags=re.I)[0].strip(" ,–—-")
        out.append(Education(id=f"edu{i}", institution=(inst or "").strip(" ,|") or "UNKNOWN INSTITUTION",
                             degree=degree_text, level=level,
                             start=_ym(dr.group(1)) if dr else None,
                             end=_ym(dr.group(2), end=True) if dr else (_ym(dates[-1], end=True) if dates else None),
                             coursework=coursework))
    return out


def _degree_level(text: str) -> str | None:
    t = text.lower()
    if re.search(r"ph\.?\s?d|doctor", t):
        return "phd"
    if "mba" in t:
        return "mba"
    if re.search(r"master|m\.?\s?tech|m\.?\s?s\.?c?\b|m\.?\s?e\b|m\.?c\.?a", t):
        return "master"
    if re.search(r"bachelor|b\.?\s?tech|b\.?\s?e\b|b\.?\s?s\.?c?\b|b\.?a\b|b\.?c\.?a", t):
        return "bachelor"
    if "diploma" in t:
        return "diploma"
    if "associate" in t:
        return "associate"
    return None


def parse_skills(lines: list[str]) -> list[SkillEntry]:
    names: list[str] = []
    for ln in lines:
        ln = _strip_bullet(ln)
        if ":" in ln:
            ln = ln.split(":", 1)[1]
        for part in re.split(r",|;|\||•|·|/(?=\s)", ln):
            p = part.strip(" .")
            if 1 <= len(p) <= 40:
                names.append(canonical(p))
    seen, out = set(), []
    for n in names:
        if n.lower() not in seen:
            seen.add(n.lower())
            out.append(SkillEntry(name=n))
    return out


def parse_projects(lines: list[str]) -> list[Project]:
    projects: list[Project] = []
    for ln in lines:
        if _is_bullet(ln) and projects:
            pid = projects[-1].id
            projects[-1].bullets.append(Bullet(id=f"{pid}.b{len(projects[-1].bullets) + 1}", text=_strip_bullet(ln)))
        elif projects and projects[-1].bullets and ln[:1].islower():
            projects[-1].bullets[-1].text += " " + ln
        else:
            if ":" in ln[:60]:
                name, desc = ln.split(":", 1)
            else:
                m = re.split(r"\s+[—–-]\s+", ln, maxsplit=1)
                name, desc = (m[0], m[1]) if len(m) == 2 else (ln, "")
            pid = f"proj{len(projects) + 1}"
            projects.append(Project(id=pid, name=name.strip(), description=(desc or "").strip()))
    return projects


def parse_certifications(lines: list[str]) -> list[Certification]:
    out = []
    for i, ln in enumerate(lines, start=1):
        t = _strip_bullet(ln)
        date = SINGLE_DATE.search(t)
        name = re.sub(r"\(?\b" + re.escape(date.group(0)) + r"\b\)?", "", t).strip(" ,–—-|") if date else t
        issuer = None
        if " — " in name or " - " in name or ", " in name:
            name, issuer = [x.strip() for x in re.split(r"\s+[—-]\s+|,\s+", name, maxsplit=1)]
        out.append(Certification(id=f"cert{i}", name=name, issuer=issuer, date=_ym(date.group(0)) if date else None))
    return out


def parse_resume(path: str | Path) -> tuple[Profile, str]:
    """Return (draft profile, extracted text). Personal fields beyond the resume stay empty."""
    text = extract_text(path)
    sections = split_sections(text)
    if "experience" not in sections and "education" not in sections:
        raise ResumeParseError("no Experience or Education section found; check the resume headings")
    contact = parse_contact(sections.get("header", []), text)
    experience = parse_experience(sections.get("experience", []))
    skills = parse_skills(sections.get("skills", []))
    # Skills used in bullets but missing from the skills section remain evidence via the bullets;
    # they are not added to the skills list automatically (see bullet_skill_gaps).
    achievements = [Bullet(id=f"ach{i}", text=_strip_bullet(t)) for i, t in enumerate(sections.get("achievements", []), 1)]
    profile = Profile(
        contact=contact,
        summary=" ".join(sections.get("summary", [])) or None,
        experience=experience,
        education=parse_education(sections.get("education", [])),
        certifications=parse_certifications(sections.get("certifications", [])),
        projects=parse_projects(sections.get("projects", [])),
        achievements=achievements,
        skills=skills,
        documents={"master_resume": str(Path(path).resolve())},
    )
    return profile, text


def bullet_skill_gaps(profile: Profile) -> list[str]:
    """Skills demonstrated in bullets but absent from the skills list (for the candidate to confirm)."""
    listed = {s.name for s in profile.skills}
    used: set[str] = set()
    for b in profile.all_bullets():
        used |= extract_skills(b.text)
    return sorted(used - listed)
