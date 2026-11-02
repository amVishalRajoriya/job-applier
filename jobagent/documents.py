"""Render resume versions (DOCX, TXT, HTML, PDF) and cover letters."""
from __future__ import annotations

import html
import os
from pathlib import Path

from .profile import Profile
from .requirements import JobRequirements
from .resume_doc import ResumeDoc
from .text import overlap
from .truth_guard import check_free_text


def _dates(start: str | None, end: str | None) -> str:
    def fmt(x: str | None) -> str:
        if not x:
            return "Present"
        y, m = x.split("-")[:2]
        return f"{['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][int(m) - 1]} {y}"
    return f"{fmt(start)} – {fmt(end)}" if start else fmt(end)


def _skill_line(doc: ResumeDoc) -> str:
    return ", ".join(f"{s} ({doc.skill_labels[s]})" if doc.skill_labels.get(s) else s for s in doc.skills)


def render_text(doc: ResumeDoc) -> str:
    c = doc.contact
    lines = [c.get("name", ""), " | ".join(x for x in [c.get("location"), c.get("email"), c.get("phone"), c.get("linkedin"),
                                                         c.get("github"), c.get("website")] if x), ""]
    if doc.summary:
        lines += ["SUMMARY", doc.summary, ""]
    lines.append("EXPERIENCE")
    for r in doc.roles:
        lines.append(f"{r.title} — {r.employer}{' | ' + r.location if r.location else ''} | {_dates(r.start, r.end)}")
        lines += [f"- {b.text}" for b in r.bullets]
        lines.append("")
    if doc.projects:
        lines.append("PROJECTS")
        for p in doc.projects:
            lines.append(p.name)
            lines += [f"- {b.text}" for b in p.bullets]
        lines.append("")
    lines.append("EDUCATION")
    for e in doc.education:
        lines.append(f"{e.degree} — {e.institution}{' | ' + e.end[:4] if e.end else ''}")
        if e.coursework:
            lines.append("Relevant coursework: " + ", ".join(e.coursework))
    lines.append("")
    if doc.skills:
        lines += ["SKILLS", _skill_line(doc), ""]
    if doc.certifications:
        lines.append("CERTIFICATIONS")
        lines += [f"{c.name}{' — ' + c.issuer if c.issuer else ''}{' (' + c.date[:4] + ')' if c.date else ''}" for c in doc.certifications]
    return "\n".join(lines).strip() + "\n"


def render_html(doc: ResumeDoc) -> str:
    body = []
    for line in render_text(doc).splitlines():
        e = html.escape(line)
        if line.isupper() and line:
            body.append(f"<h2>{e}</h2>")
        elif line.startswith("- "):
            body.append(f"<li>{html.escape(line[2:])}</li>")
        elif line:
            body.append(f"<p>{e}</p>")
    return ("<!doctype html><html><head><meta charset='utf-8'><title>Resume</title>"
            "<style>body{font-family:Arial,sans-serif;max-width:780px;margin:24px auto;font-size:14px}"
            "h2{font-size:15px;border-bottom:1px solid #999;margin-top:18px}p{margin:4px 0}li{margin:2px 0 2px 18px}</style>"
            "</head><body>" + "".join(body) + "</body></html>")


def render_docx(doc: ResumeDoc, path: Path) -> Path:
    """Single-column, ATS-friendly DOCX: real headings, plain bullets, no tables or text boxes."""
    import docx
    from docx.shared import Pt
    d = docx.Document()
    style = d.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10.5)
    c = doc.contact
    d.add_heading(c.get("name", ""), level=0)
    d.add_paragraph(" | ".join(x for x in [c.get("location"), c.get("email"), c.get("phone"), c.get("linkedin"),
                                           c.get("github"), c.get("website")] if x))
    if doc.summary:
        d.add_heading("Summary", level=1)
        d.add_paragraph(doc.summary)
    d.add_heading("Experience", level=1)
    for r in doc.roles:
        p = d.add_paragraph()
        p.add_run(f"{r.title} — {r.employer}").bold = True
        p.add_run(f"{' | ' + r.location if r.location else ''} | {_dates(r.start, r.end)}")
        for b in r.bullets:
            d.add_paragraph(b.text, style="List Bullet")
    if doc.projects:
        d.add_heading("Projects", level=1)
        for pr in doc.projects:
            d.add_paragraph().add_run(pr.name).bold = True
            for b in pr.bullets:
                d.add_paragraph(b.text, style="List Bullet")
    d.add_heading("Education", level=1)
    for e in doc.education:
        d.add_paragraph(f"{e.degree} — {e.institution}{' | ' + e.end[:4] if e.end else ''}")
        if e.coursework:
            d.add_paragraph("Relevant coursework: " + ", ".join(e.coursework))
    if doc.skills:
        d.add_heading("Skills", level=1)
        d.add_paragraph(_skill_line(doc))
    if doc.certifications:
        d.add_heading("Certifications", level=1)
        for ct in doc.certifications:
            d.add_paragraph(f"{ct.name}{' — ' + ct.issuer if ct.issuer else ''}{' (' + ct.date[:4] + ')' if ct.date else ''}")
    path.parent.mkdir(parents=True, exist_ok=True)
    d.save(str(path))
    return path


def render_pdf(html_text: str, path: Path) -> Path | None:
    """PDF via headless Chromium. Best effort: returns None if the browser is unavailable."""
    try:
        from .browser import launch_chromium
        with launch_chromium(headless=True) as (_, browser):
            page = browser.new_page(java_script_enabled=False)
            page.route("**/*", lambda route: route.abort())  # the resume HTML is self-contained
            page.set_content(html_text)
            page.pdf(path=str(path), format="A4", margin={"top": "14mm", "bottom": "14mm", "left": "14mm", "right": "14mm"})
        return path
    except Exception:
        return None


def write_version(doc: ResumeDoc, out_dir: Path, stem: str, pdf: bool = False) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(out_dir, 0o700)
    except OSError:
        pass
    files = {
        "docx": str(render_docx(doc, out_dir / f"{stem}.docx")),
        "txt": str(out_dir / f"{stem}.txt"),
        "html": str(out_dir / f"{stem}.html"),
    }
    Path(files["txt"]).write_text(render_text(doc))
    Path(files["html"]).write_text(render_html(doc))
    if pdf and render_pdf(render_html(doc), out_dir / f"{stem}.pdf"):
        files["pdf"] = str(out_dir / f"{stem}.pdf")
    return files


def template_cover_letter(profile: Profile, doc: ResumeDoc, job: dict, reqs: JobRequirements) -> str:
    """Deterministic letter built only from verified resume lines (used when no model is configured)."""
    years = profile.total_months() // 12
    jd = "\n".join(r.text for r in reqs.requirements)
    top = sorted(doc.bullets(), key=lambda b: overlap(b.text, jd), reverse=True)[:3]
    recent = doc.roles[0] if doc.roles else None
    doms = sorted(set(reqs.domains) & doc.domains(profile))
    intro = (f"I am applying for the {job['title']} role at {job['employer']}. "
             + (f"I am a {recent.title} at {recent.employer} with {years} years of experience"
                + (f" in {' and '.join(doms)}" if doms else "") + "." if recent else ""))
    middle = "Experience from my resume that is most relevant to this role:\n" + "\n".join(f"- {b.text}" for b in top)
    close = "Thank you for considering my application. I would welcome the chance to discuss the role."
    letter = f"{intro}\n\n{middle}\n\n{close}\n\n{profile.contact.name}"
    problems = check_free_text(letter, profile, extra_allowed_text=f"{job['employer']} {job['title']}",
                               quoted=(job["title"], job["employer"]))
    if problems:  # should not happen: every line is verified evidence
        raise ValueError("cover letter failed truth guard: " + "; ".join(problems))
    return letter
