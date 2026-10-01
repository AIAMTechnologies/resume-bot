"""Tailor the master list to one job, verify truthfulness, render, and ATS-check."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import settings
from ..llm import complete_json
from ..models import Job
from ..profile import master
from . import ats, render

TAILOR_SYSTEM = """You are an expert resume writer who optimizes for Applicant Tracking Systems
AND for human recruiters. You tailor a candidate's master profile to one job posting.

Absolute rules (violations make the resume unusable):
1. Use ONLY facts from the master profile. Never invent employers, titles, dates, degrees,
   certifications, metrics, team sizes, or technologies. Never change dates or titles.
2. You MAY rephrase bullets to mirror the job's terminology when the underlying fact supports
   it (e.g. master says "built REST endpoints in FastAPI" → "Designed RESTful APIs in Python/FastAPI").
3. Skills listed must appear in the master profile. If the job wants a skill the candidate
   lacks, leave it out and report it in "unsupported_keywords".
4. Every rewritten bullet cites the master item id(s) it came from.
5. Include ALL experience roles (no employment gaps) but give the most bullets to relevant ones.
   Pick the 2–4 most relevant projects.
6. Length: the resume MUST fit on two pages. Caps: current role 6 bullets, other roles 4,
   roles older than 5 years 2, each project 2 bullets, 5 skill categories of at most 10 items.
   Prefer fewer, stronger, job-relevant bullets over completeness.
7. ATS wording: for every job keyword the master profile supports, use the job's EXACT wording at
   least once (in Skills, or in a bullet whose source supports it). Mirror the job's terms for
   titles of skill categories where natural. Use the standard headings only.
8. Items listed under "SKILLS-SECTION-ONLY" in the prompt are real skills the candidate has, but
   with no recorded employer. List them in Skills using the job's exact wording; NEVER attribute
   them to an employer or project in a bullet, and never cite their ids as a bullet source."""

TAILOR_FORMAT = """Return JSON:
{
 "jd_keywords": ["the 15-25 most important hard skills/tools/qualifications from the job, as written in the job"],
 "headline": "short professional headline matching the target role (truthful)",
 "summary": "2-3 sentence summary tailored to this job",
 "skills": [{"category": "Languages", "items": ["..."]}, ...],
 "experience": [{"source_ids": [1], "title": "", "organization": "", "location": "", "start": "", "end": "",
                 "bullets": [{"text": "...", "source_ids": [1]}]}],
 "projects": [{"source_ids": [7], "title": "", "organization": "", "start": "", "end": "", "stack": ["..."],
               "bullets": [{"text": "...", "source_ids": [7]}]}],
 "education": [{"source_ids": [3], "title": "", "organization": "", "start": "", "end": "", "bullets": []}],
 "certifications": [{"source_ids": [4], "title": "", "organization": "", "start": "", "end": ""}],
 "unsupported_keywords": ["job keywords the candidate cannot truthfully claim"]
}"""


@dataclass
class TailoredResume:
    content: dict[str, Any]
    docx: Path
    pdf: Path
    report: ats.ATSReport
    jd_keywords: list[str]
    unsupported: list[str]
    dropped: list[str]


_NUM = re.compile(r"\d[\d,.]*\s*%?|\$\s?\d[\d,.]*[kKmM]?")


def _verify(content: dict, items_by_id: dict[int, Any]) -> list[str]:
    """Deterministic truth checks. Mutates content; returns a list of dropped/replaced things."""
    dropped: list[str] = []
    skills_only_ids = {i for i, it in items_by_id.items()
                       if any("skills section only" in src for src in getattr(it, "sources", []) or [])}
    known_skills = {ats.normalize(s).strip() for s in master.all_skills()}
    evidence = ats.normalize(" ".join(
        " ".join([i.title, *i.bullets, *i.skills]) for i in items_by_id.values()))
    evidence_words = set(re.split(r"[^a-z0-9+#.]+", evidence))
    stop = {"and", "of", "the", "for", "with", "in", "to", "a", "e", "g"}

    def known(skill: str) -> bool:
        n = ats.normalize(skill).strip()
        if n in known_skills or any(ats.has_keyword(f" {k} ", skill) for k in known_skills):
            return True
        # A rewording is fine when every meaningful word is evidenced somewhere in the profile
        # ("Security Metrics", "Network Segmentation"); a new tool or skill is not.
        # Word order doesn't matter ("IDS/IPS" = "IPS/IDS").
        words = [w for w in re.split(r"[^a-z0-9+#.]+", n) if w and w not in stop]
        return bool(words) and all(w in evidence_words for w in words)

    for group in content.get("skills", []):
        keep = [s for s in group.get("items", []) if known(s)]
        dropped += [f"skill:{s}" for s in group.get("items", []) if s not in keep]
        group["items"] = keep
    content["skills"] = [g for g in content.get("skills", []) if g["items"]]

    for section in ("experience", "projects", "education", "certifications"):
        entries = []
        for e in content.get(section, []):
            ids = [i for i in e.get("source_ids", []) if i in items_by_id]
            if not ids:
                dropped.append(f"{section}:{e.get('title')} (no source)")
                continue
            src = items_by_id[ids[0]]
            # Titles, orgs, and dates always come from the master record, never the model.
            e["title"], e["organization"] = src.title, src.organization
            e["start"], e["end"] = src.start, src.end
            if section == "experience":
                e["location"] = src.location
            source_text = " ".join(" ".join(items_by_id[i].bullets) for i in ids if i in items_by_id)
            source_nums = {n.strip() for n in _NUM.findall(source_text)}
            clean = []
            for b in e.get("bullets", []):
                text = b["text"] if isinstance(b, dict) else str(b)
                cited = set(b.get("source_ids", [])) if isinstance(b, dict) else set()
                if cited & skills_only_ids:
                    dropped.append(f"bullet attributing a skills-only item to {src.organization or src.title}: {text[:80]}")
                    continue
                new_nums = {n.strip() for n in _NUM.findall(text)} - source_nums
                if new_nums:
                    dropped.append(f"bullet with unsupported numbers {sorted(new_nums)}: {text[:80]}")
                    continue
                clean.append(text)
            e["bullets"] = clean
            if section == "projects":
                e["stack"] = [s for s in e.get("stack", []) if known(s)]
            entries.append(e)
        content[section] = entries
    _backfill_keywords(content, known)
    return dropped


def _resume_text(content: dict) -> str:
    parts = [content.get("headline", ""), content.get("summary", "")]
    for g in content.get("skills", []):
        parts += g.get("items", [])
    for section in ("experience", "projects", "education", "certifications"):
        for e in content.get(section, []):
            parts += [e.get("title", ""), *[b if isinstance(b, str) else b.get("text", "") for b in e.get("bullets", [])],
                      *e.get("stack", [])]
    return " ".join(p for p in parts if p)


def _backfill_keywords(content: dict, known) -> list[str]:
    """Add job keywords the profile genuinely supports but the draft left out, word for word.

    This is what lifts ATS keyword coverage honestly: nothing is added that fails the same
    evidence check applied to every other skill.
    """
    text = ats.normalize(_resume_text(content))
    unsupported = {ats.normalize(u).strip() for u in content.get("unsupported_keywords", [])}
    added = [k for k in content.get("jd_keywords", [])
             if not ats.has_keyword(text, k) and ats.normalize(k).strip() not in unsupported and known(k)]
    if added:
        groups = content.setdefault("skills", [])
        extra = next((g for g in groups if g.get("category") == "Additional Skills"), None)
        if extra is None:
            extra = {"category": "Additional Skills", "items": []}
            groups.append(extra)
        extra["items"] = [*extra["items"], *added][:14]
    return added


async def tailor(job: Job, contact: dict, extra_instruction: str = "") -> TailoredResume:
    items = master.all_items()
    items_by_id = {i.id: i for i in items}
    context = f"MASTER PROFILE (ids in brackets):\n{master.render(items)}"
    skills_only = [i for i in items if any("skills section only" in src for src in i.sources)]
    if skills_only:
        extra_instruction = ("SKILLS-SECTION-ONLY item ids: " + ", ".join(str(i.id) for i in skills_only)
                             + "\n" + extra_instruction)
    prompt = f"""JOB: {job.title} at {job.company} ({job.location})
<<<
{job.description[:15000]}
>>>
{extra_instruction}
{TAILOR_FORMAT}"""
    content = await complete_json(prompt, system=TAILOR_SYSTEM, max_tokens=8000, context=context)
    dropped = _verify(content, items_by_id)
    keywords = content.get("jd_keywords", [])

    docx_path, pdf_path = render.output_paths(contact, job.company, job.id or 0)
    render.render_docx(content, contact, docx_path)
    render.render_pdf(content, contact, pdf_path)
    report = ats.check(pdf_path, docx_path, keywords, contact)
    return TailoredResume(content, docx_path, pdf_path, report, keywords,
                          content.get("unsupported_keywords", []), dropped)


def _pages(result: "TailoredResume") -> int:
    from pypdf import PdfReader
    return len(PdfReader(str(result.pdf)).pages)


def _trim_to_fit(result: "TailoredResume", job: Job, contact: dict, max_pages: int = 2) -> "TailoredResume":
    """Deterministic last resort: drop the least important bullets until the PDF fits.

    Order: extra projects → project bullets → bullets of the oldest roles (never below 2) →
    bullets of newer roles (never below 3). Never removes a job, so there are no gaps.
    """
    c = result.content

    def render_and_count() -> int:
        render.render_pdf(c, contact, result.pdf)
        return _pages(result)

    def steps():
        while len(c.get("projects", [])) > 2:
            c["projects"].pop()
            yield
        for proj in reversed(c.get("projects", [])):
            while len(proj.get("bullets", [])) > 1:
                proj["bullets"].pop()
                yield
        roles = c.get("experience", [])
        for role in reversed(roles[2:]):  # older roles first, down to 2 bullets
            while len(role.get("bullets", [])) > 2:
                role["bullets"].pop()
                yield
        while True:  # then shave the longest of the two newest roles, down to 3 bullets
            longest = max(roles[:2], key=lambda r: len(r.get("bullets", [])), default=None)
            if not longest or len(longest.get("bullets", [])) <= 3:
                break
            longest["bullets"].pop()
            yield
        while c.get("projects"):
            c["projects"].pop()
            yield

    for _ in steps():
        if render_and_count() <= max_pages:
            break
    render.render_docx(c, contact, result.docx)
    result.report = ats.check(result.pdf, result.docx, result.jd_keywords, contact)
    return result


async def tailor_with_retry(job: Job, contact: dict) -> TailoredResume:
    cfg = settings().ats
    result = await tailor(job, contact)
    # One AI call by default: length is fixed deterministically (_trim_to_fit) and supported
    # keywords are backfilled in _verify. Extra AI rewrites only if retailor_attempts > 0.
    for _ in range(2 if cfg.retailor_attempts else 0):
        pages = _pages(result)
        if pages <= 2:
            break
        result = await tailor(job, contact, extra_instruction=(
            f"A previous draft ran {pages} pages. It must fit on 2: cut the least job-relevant bullets "
            "and projects, and use fewer bullets for older roles."))
    if _pages(result) > 2:
        result = _trim_to_fit(result, job, contact)
    best, attempts = result, 0
    while best.report.keyword_coverage < cfg.min_keyword_coverage and attempts < cfg.retailor_attempts:
        attempts += 1
        supportable = [k for k in best.report.missing if k not in best.unsupported]
        if not supportable:
            break
        result = await tailor(job, contact, extra_instruction=(
            "A previous draft missed these job keywords that the master profile CAN support — work "
            f"them in naturally where truthful: {', '.join(supportable)}"))
        if _pages(result) > 2:
            result = _trim_to_fit(result, job, contact)
        if result.report.keyword_coverage > best.report.keyword_coverage:
            best = result
    if best is not result:
        # Every draft is written to the same files; put the best one back on disk.
        render.render_pdf(best.content, contact, best.pdf)
        render.render_docx(best.content, contact, best.docx)
        best.report = ats.check(best.pdf, best.docx, best.jd_keywords, contact)
    return best


OUTREACH_SYSTEM = """Write a LinkedIn connection note (max 280 characters) from a candidate to the
hiring manager or recruiter for a role they just applied to. Specific, warm, no flattery, no
clichés, one concrete proof point from the profile, ends with a light ask. Facts from the
profile only."""


async def outreach_note(job: Job) -> str:
    from ..llm import get_llm
    note = (await get_llm().complete(
        f"ROLE: {job.title} at {job.company}\n{job.description[:3000]}\n\nWrite only the note.",
        system=OUTREACH_SYSTEM, max_tokens=300, context=master.prompt_context())).strip()
    return note[:300]


COVER_SYSTEM = """Write concise, specific cover letters (180-250 words). Only use facts from the
candidate profile. No clichés ("I am writing to express"), no flattery, no invented facts.
Structure: why this role/company (specific to the posting) → 2 concrete proofs from the
profile → short close."""


async def cover_letter(job: Job, contact: dict) -> str:
    from ..llm import get_llm
    name = " ".join(x for x in [contact.get("first_name"), contact.get("last_name")] if x)
    return (await get_llm().complete(
        f"CANDIDATE: {name}\nJOB: {job.title} at {job.company}\n{job.description[:8000]}\n\n"
        "Write the cover letter body only (no address block).",
        system=COVER_SYSTEM, max_tokens=1200, context=master.prompt_context())).strip()
