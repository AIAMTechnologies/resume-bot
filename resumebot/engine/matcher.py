"""Filter and score jobs against targets and the master profile."""
from __future__ import annotations

import re
from datetime import timedelta

from sqlmodel import select

from .. import db
from ..config import settings
from ..llm import complete_json
from ..models import Job, JobStatus, utcnow
from ..profile import master
from ..profile.ingest import target_titles
from ..profile.master import norm

ACTIVE_STATUSES = [JobStatus.QUEUED, JobStatus.APPLYING, JobStatus.APPLIED, JobStatus.REVIEW, JobStatus.MANUAL]
REMOTE_RE = re.compile(r"\bremote\b|anywhere|work from home|distributed", re.I)


def dedupe_key(company: str, title: str) -> str:
    title = re.sub(r"\(.*?\)|\[.*?\]| - .*$", "", title)
    return f"{norm(company)}|{norm(title)}"


def location_ok(job: Job) -> tuple[bool, str]:
    loc = settings().targets.locations
    text = f"{job.location} {job.title}"
    if job.remote or REMOTE_RE.search(text):
        if not loc.remote:
            return False, "remote not wanted"
        regions = [r.lower() for r in loc.remote_regions]
        restricted = re.search(r"remote\s*[-–(,]\s*([A-Za-z .]+)", job.location or "", re.I)
        where = restricted.group(1).lower() if restricted else ""
        if restricted and regions and not any(re.search(rf"\b{re.escape(r)}\b", where)
                                              for r in regions + ["us", "usa", "ca"]):
            return False, f"remote restricted to {restricted.group(1).strip()}"
        return True, "remote"
    low = (job.location or "").lower()
    if not low:
        return True, "no location given"
    for city in loc.cities:
        if city.split(",")[0].lower() in low:
            return True, f"city {city}"
    if any(c.lower() in low for c in loc.countries) or re.search(r"\b(canada|ontario|, on\b)", low):
        return True, "country match"
    return False, f"location {job.location}"


def title_ok(title: str, targets: list[str], keywords: list[str] | None = None) -> bool:
    if not targets:
        return True
    words = set(norm(title).split())
    if keywords and any(norm(k) in words or norm(k) in norm(title) for k in keywords):
        return True
    for t in targets:
        tw = set(norm(t).split()) - {"senior", "junior", "sr", "jr", "lead", "i", "ii", "iii"}
        if tw and len(tw & words) / len(tw) >= 0.5:
            return True
    return False


def prefilter(job: Job) -> tuple[bool, str]:
    t = settings().targets
    low = job.title.lower()
    for kw in t.exclude_title_keywords:
        if kw.lower() in low:
            return False, f"excluded keyword '{kw.strip()}'"
    if norm(job.company) in {norm(c) for c in t.exclude_companies}:
        return False, "excluded company"
    if job.posted_at and job.posted_at < utcnow() - timedelta(days=settings().matching.max_job_age_days):
        return False, "too old"
    if not title_ok(job.title, target_titles(), t.title_keywords):
        return False, "title not in targets"
    if job.source == "linkedin" and job.apply_url and not job.easy_apply:
        return False, "applies on company ATS (tracked there)"
    ok, why = location_ok(job)
    if not ok:
        return False, why
    with db.session() as s:
        dup = s.exec(select(Job).where(Job.dedupe_key == job.dedupe_key, Job.id != job.id,
                                       Job.status.in_(ACTIVE_STATUSES))).first()
    if dup:
        return False, f"duplicate of #{dup.id} ({dup.source})"
    return True, ""


SCORE_SYSTEM = """You are a precise technical recruiter screening a candidate for a job.
Score 0-100 for how likely the candidate passes a recruiter screen, given ONLY the evidence
in their profile. 85+: meets nearly all requirements. 70-84: meets most must-haves.
50-69: partial. <50: poor fit or wrong seniority. Be calibrated, not generous."""


async def score(job: Job) -> tuple[int, list[str], list[str]]:
    result = await complete_json(f"""CANDIDATE PROFILE:
{master.render(with_ids=False)[:20000]}

JOB: {job.title} at {job.company} — {job.location} {job.salary}
{job.description[:10000]}

Return {{"score": 0-100, "reasons": ["up to 4 short reasons"], "missing": ["must-have requirements the candidate lacks"],
 "hard_blocker": "empty or e.g. 'requires US citizenship / security clearance / 10+ yrs'"}}""",
        system=SCORE_SYSTEM, max_tokens=800)
    s = int(result.get("score", 0))
    reasons = list(result.get("reasons", []))
    if result.get("hard_blocker"):
        reasons.insert(0, f"Blocker: {result['hard_blocker']}")
        s = min(s, 40)
    return s, reasons, list(result.get("missing", []))
