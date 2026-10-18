"""Render the SYNTHETIC master resume to PDF and DOCX so ingestion can be tested on real file formats.

Usage: python data/sample/build_samples.py [out_dir]
"""
from __future__ import annotations

import html
import sys
from pathlib import Path

HERE = Path(__file__).parent
SOURCE = HERE / "master_resume_SYNTHETIC.md"


def _blocks():
    for line in SOURCE.read_text().splitlines():
        yield line


def build_docx(out: Path) -> Path:
    import docx
    d = docx.Document()
    for line in _blocks():
        if not line.strip():
            continue
        if line.isupper():
            d.add_heading(line.title(), level=2)
        elif line.startswith("- "):
            d.add_paragraph(line[2:], style="List Bullet")
        else:
            d.add_paragraph(line)
    path = out / "master_resume_SYNTHETIC.docx"
    d.save(str(path))
    return path


def build_pdf(out: Path) -> Path:
    from jobagent.browser import launch_chromium
    parts = ["<html><body style='font-family:Arial;font-size:11px'>"]
    in_list = False
    for line in _blocks():
        if line.startswith("- "):
            if not in_list:
                parts.append("<ul>")
                in_list = True
            parts.append(f"<li>{html.escape(line[2:])}</li>")
            continue
        if in_list:
            parts.append("</ul>")
            in_list = False
        if not line.strip():
            continue
        tag = "h3" if line.isupper() else "p"
        parts.append(f"<{tag}>{html.escape(line)}</{tag}>")
    parts.append("</body></html>")
    path = out / "master_resume_SYNTHETIC.pdf"
    with launch_chromium() as (_, browser):
        page = browser.new_page()
        page.set_content("".join(parts))
        page.pdf(path=str(path), format="A4")
    return path


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE
    out.mkdir(parents=True, exist_ok=True)
    print(build_docx(out))
    print(build_pdf(out))
