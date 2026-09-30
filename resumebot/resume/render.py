"""Render a tailored resume to ATS-safe DOCX and PDF.

ATS-safe means: single column, no tables/images/text boxes, nothing in headers/footers,
standard section names, standard fonts, real text (not outlines), consistent date format.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..config import DATA_DIR

SECTION_ORDER = [("summary", "Summary"), ("skills", "Skills"), ("experience", "Experience"),
                 ("projects", "Projects"), ("education", "Education"),
                 ("certifications", "Certifications")]


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:40]


MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def _fmt_date(d: str) -> str:
    m = re.fullmatch(r"(\d{4})-(\d{1,2})(?:-\d{1,2})?", (d or "").strip())
    return f"{MONTHS[int(m.group(2)) - 1]} {m.group(1)}" if m and 1 <= int(m.group(2)) <= 12 else (d or "")


def _dates(e: dict) -> str:
    start, end = _fmt_date(e.get("start", "")), _fmt_date(e.get("end", ""))
    return f"{start} – {end}" if start and end else (start or end)


def _url(u: str) -> str:
    """linkedin.com/in/x style: no scheme, no www, no tracking query."""
    u = re.sub(r"^https?://(www\.)?", "", (u or "").strip())
    return u.split("?")[0].rstrip("/")


def _contact_line(c: dict) -> str:
    loc = ", ".join(x for x in [c.get("city"), c.get("province_state")] if x)
    return "  |  ".join(x for x in [loc, c.get("phone"), c.get("email"), _url(c.get("linkedin", "")),
                                    _url(c.get("github", "")), _url(c.get("portfolio", ""))] if x)


def output_paths(contact: dict, company: str, job_id: int) -> tuple[Path, Path]:
    name = _safe(f"{contact.get('first_name', '')}_{contact.get('last_name', '')}") or "Resume"
    folder = DATA_DIR / "resumes" / f"{job_id}_{_safe(company)}"
    folder.mkdir(parents=True, exist_ok=True)
    base = folder / f"{name}_Resume"
    return base.with_suffix(".docx"), base.with_suffix(".pdf")


def render_docx(resume: dict[str, Any], contact: dict, path: Path) -> Path:
    import docx
    from docx.enum.text import WD_TAB_ALIGNMENT
    from docx.shared import Inches, Pt

    d = docx.Document()
    for s in d.sections:
        s.left_margin = s.right_margin = Inches(0.7)
        s.top_margin = s.bottom_margin = Inches(0.6)
    style = d.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10.5)
    style.paragraph_format.space_after = Pt(2)
    right_tab = Inches(7.1)

    def para(text: str = "", bold: bool = False, size: float | None = None, after: float = 2):
        p = d.add_paragraph()
        p.paragraph_format.space_after = Pt(after)
        if text:
            r = p.add_run(text)
            r.bold = bold
            if size:
                r.font.size = Pt(size)
        return p

    full_name = " ".join(x for x in [contact.get("first_name"), contact.get("last_name")] if x)
    para(full_name, bold=True, size=16, after=0)
    if resume.get("headline"):
        para(resume["headline"], size=11, after=0)
    para(_contact_line(contact), after=6)

    def heading(title: str):
        p = para(title.upper(), bold=True, size=11.5, after=2)
        p.paragraph_format.space_before = Pt(6)

    def entry(left: str, right: str, sub: str = ""):
        p = d.add_paragraph()
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.tab_stops.add_tab_stop(right_tab, WD_TAB_ALIGNMENT.RIGHT)
        p.add_run(left).bold = True
        if right:
            p.add_run(f"\t{right}")
        if sub:
            para(sub, after=1).runs[0].italic = True

    def bullets(items: list[str]):
        for b in items:
            p = d.add_paragraph(b, style="List Bullet")
            p.paragraph_format.space_after = Pt(1)

    for key, title in SECTION_ORDER:
        val = resume.get(key)
        if not val:
            continue
        heading(title)
        if key == "summary":
            para(val)
        elif key == "skills":
            for group in val:
                p = d.add_paragraph()
                p.paragraph_format.space_after = Pt(1)
                p.add_run(f"{group['category']}: ").bold = True
                p.add_run(", ".join(group["items"]))
        elif key in ("experience", "projects"):
            for e in val:
                left = e.get("title", "")
                if e.get("organization"):
                    left += f" — {e['organization']}"
                sub = e.get("location", "") if key == "experience" else ", ".join(e.get("stack", []))
                entry(left, _dates(e), sub)
                bullets(e.get("bullets", []))
        elif key in ("education", "certifications"):
            for e in val:
                left = ", ".join(x for x in [e.get("title"), e.get("organization")] if x)
                entry(left, _dates(e))
                bullets(e.get("bullets", []))
    d.save(str(path))
    return path


def render_pdf(resume: dict[str, Any], contact: dict, path: Path) -> Path:
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer, Table

    from xml.sax.saxutils import escape as esc

    base = ParagraphStyle("base", fontName="Helvetica", fontSize=10, leading=12.5, alignment=TA_LEFT)
    bold = ParagraphStyle("bold", parent=base, fontName="Helvetica-Bold")
    name = ParagraphStyle("name", parent=bold, fontSize=16, leading=19)
    head = ParagraphStyle("head", parent=bold, fontSize=11, leading=14, spaceBefore=7, spaceAfter=2)
    small = ParagraphStyle("small", parent=base, fontName="Helvetica-Oblique", fontSize=9.5)

    story: list = []
    full_name = " ".join(x for x in [contact.get("first_name"), contact.get("last_name")] if x)
    story.append(Paragraph(esc(full_name), name))
    if resume.get("headline"):
        story.append(Paragraph(esc(resume["headline"]), base))
    story.append(Paragraph(esc(_contact_line(contact)), base))
    story.append(Spacer(1, 4))

    width = LETTER[0] - 1.4 * inch

    def entry_row(left: str, right: str):
        # A 2-cell row keeps dates right-aligned; reportlab emits it as plain text in reading order.
        t = Table([[Paragraph(f"<b>{esc(left)}</b>", base), Paragraph(esc(right), base)]],
                  colWidths=[width * 0.75, width * 0.25], hAlign="LEFT")
        t.setStyle([("ALIGN", (1, 0), (1, 0), "RIGHT"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 1),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0), ("VALIGN", (0, 0), (-1, -1), "TOP")])
        story.append(t)

    def bullet_list(items: list[str]):
        if items:
            story.append(ListFlowable([ListItem(Paragraph(esc(b), base), leftIndent=10) for b in items],
                                      bulletType="bullet", start="•", leftIndent=12, bulletFontSize=8))

    for key, title in SECTION_ORDER:
        val = resume.get(key)
        if not val:
            continue
        story.append(Paragraph(title.upper(), head))
        if key == "summary":
            story.append(Paragraph(esc(val), base))
        elif key == "skills":
            for g in val:
                story.append(Paragraph(f"<b>{esc(g['category'])}:</b> {esc(', '.join(g['items']))}", base))
        elif key in ("experience", "projects"):
            for e in val:
                left = e.get("title", "") + (f" — {e['organization']}" if e.get("organization") else "")
                entry_row(left, _dates(e))
                sub = e.get("location", "") if key == "experience" else ", ".join(e.get("stack", []))
                if sub:
                    story.append(Paragraph(esc(sub), small))
                bullet_list(e.get("bullets", []))
                story.append(Spacer(1, 3))
        else:
            for e in val:
                entry_row(", ".join(x for x in [e.get("title"), e.get("organization")] if x), _dates(e))
                bullet_list(e.get("bullets", []))

    doc = SimpleDocTemplate(str(path), pagesize=LETTER, leftMargin=0.7 * inch, rightMargin=0.7 * inch,
                            topMargin=0.55 * inch, bottomMargin=0.55 * inch,
                            title=f"{full_name} Resume", author=full_name)
    doc.build(story)
    return path


def from_master(max_projects: int = 4) -> dict[str, Any]:
    """An untailored resume straight from the master list (for review and as a fallback)."""
    from ..profile import master
    items = master.all_items()
    by = lambda k: [i for i in items if i.kind == k]  # noqa: E731
    summary = by("summary")
    entry = lambda i, **extra: {"title": i.title, "organization": i.organization, "location": i.location,  # noqa: E731
                                "start": i.start, "end": i.end, "bullets": list(i.bullets), **extra}
    return {
        "summary": " ".join(summary[0].bullets[:2]) if summary else "",
        "skills": [{"category": i.title, "items": i.skills[:14]} for i in by("skill")],
        "experience": [entry(i) for i in by("experience")],
        "projects": [entry(i, stack=i.skills[:6], bullets=i.bullets[:2]) for i in by("project")[:max_projects]],
        "education": [entry(i, bullets=[]) for i in by("education")],
        "certifications": [entry(i) for i in by("certification")],
    }
