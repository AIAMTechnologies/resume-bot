"""Filter and score jobs against targets and the master profile."""
from __future__ import annotations

import re
from datetime import timedelta

from sqlmodel import select

from .. import db
from ..config import env, settings
from ..llm import complete_json
from ..models import Job, JobStatus, utcnow
from ..profile import master
from ..profile.ingest import target_titles
from ..profile.master import norm
from .geography import is_us_location

ACTIVE_STATUSES = [JobStatus.QUEUED, JobStatus.APPLYING, JobStatus.APPLIED, JobStatus.REVIEW, JobStatus.MANUAL]
REMOTE_RE = re.compile(r"\bremote\b|anywhere|work from home|distributed", re.I)


def dedupe_key(company: str, title: str) -> str:
    title = re.sub(r"\(.*?\)|\[.*?\]| - .*$", "", title)
    return f"{norm(company)}|{norm(title)}"


def _place_allowed(place: str, loc) -> bool:
    low = place.lower()
    if any(city.split(",")[0].lower() in low for city in loc.cities):
        return True
    if "united states" in [c.lower() for c in loc.countries] and is_us_location(place):
        return True
    return any(c.lower() in low for c in loc.countries) or bool(
        re.search(r"\b(canada|ontario|quebec|british columbia|alberta|north america|\bon\b)", low))


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
        # "Remote" attached to a specific place (e.g. "London, UK" + remote flag) is usually
        # remote within that country — only accept places you can work from.
        place = re.sub(r"\b(remote|hybrid|anywhere|worldwide|work from home|distributed)\b|[-–(),|·/]", " ",
                       job.location or "", flags=re.I).strip()
        if place and not (_place_allowed(place, loc) or any(re.search(rf"\b{re.escape(r)}\b", place, re.I)
                                                           for r in loc.remote_regions)):
            return False, f"remote but based in {job.location}"
        return True, "remote"
    low = (job.location or "").lower()
    if not low:
        return True, "no location given"
    for city in loc.cities:
        if city.split(",")[0].lower() in low:
            return True, f"city {city}"
    if "united states" in [c.lower() for c in loc.countries] and is_us_location(job.location or ""):
        return True, "United States relocation accepted"
    if any(c.lower() in low for c in loc.countries) or re.search(r"\b(canada|ontario|, on\b)", low):
        return True, "country match"
    return False, f"location {job.location}"


GENERIC_TITLE_WORDS = {"senior", "junior", "sr", "jr", "lead", "staff", "principal", "i", "ii", "iii", "iv",
                       "engineer", "analyst", "manager", "specialist", "consultant", "associate", "director",
                       "head", "officer", "architect", "administrator", "developer", "of", "and", "the"}


def title_ok(title: str, targets: list[str], keywords: list[str] | None = None) -> bool:
    if not targets:
        return True
    t_norm = norm(title)
    words = set(t_norm.split())
    # Whole-word keyword match ("soc" must not match "aSSOCiate").
    if keywords and any(re.search(rf"\b{re.escape(norm(k))}\b", t_norm) for k in keywords if norm(k)):
        return True
    for t in targets:
        tw = set(norm(t).split()) - {"senior", "junior", "sr", "jr", "lead", "i", "ii", "iii"}
        shared = tw & words
        # Sharing only generic words ("Engineer") isn't a match: "Sales Engineer" ≠ "Security Engineer".
        if tw and len(shared) / len(tw) >= 0.5 and shared - GENERIC_TITLE_WORDS:
            return True
    return False


def salary_ok(job: Job) -> tuple[bool, str]:
    from .salary import below_floor
    floor = settings().targets.min_salary or settings().targets.min_salary_cad
    low, top = below_floor(job.salary, job.description, floor)
    return (False, f"pay below ${floor:,} (posted up to ${top:,.0f})") if low else (True, "")


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
    ok, why = salary_ok(job)
    if not ok:
        return False, why
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
50-69: partial. <50: poor fit or wrong seniority. Be calibrated, not generous.
Use the CANDIDATE LOGISTICS block for location and work authorization. Do not treat a US or
other listed location as a blocker when the logistics say the candidate can work there or will
relocate. Only flag authorization as a hard blocker when the job itself requires something the
logistics rule out (e.g. US citizenship, security clearance, export-control/ITAR eligibility)."""


def candidate_logistics() -> str:
    """Work authorization and relocation facts from answers.yaml, for the scorer."""
    from ..config import answers
    a = answers()
    wa, lg, c = a.get("work_authorization", {}), a.get("logistics", {}), a.get("contact", {})
    custom = a.get("custom", {})
    lines = [
        f"Based in: {', '.join(x for x in [c.get('city'), c.get('province_state'), c.get('country')] if x)}",
        f"Authorized to work in Canada: {wa.get('canada', '')}; needs sponsorship in Canada: {wa.get('requires_sponsorship_canada', '')}",
        f"Authorized to work in the US today: {wa.get('united_states', '')}; needs US sponsorship: {wa.get('requires_sponsorship_us', '')}",
        f"Citizenship: {custom.get('What is your citizenship?', '')}",
        f"US visa route: {custom.get('Please describe your visa or work authorization status', '')}",
        f"Relocation: {wa.get('relocation_details') or wa.get('willing_to_relocate', '')}",
        f"Open to remote/hybrid/on-site: {lg.get('open_to_remote', '')}/{lg.get('open_to_hybrid', '')}/{lg.get('open_to_onsite', '')}",
    ]
    return "CANDIDATE LOGISTICS:\n" + "\n".join(line for line in lines if not line.endswith(": "))


SECOND_OPINION_FROM = 55  # fast-model score at/above which the main model re-scores


async def score(job: Job) -> tuple[int, list[str], list[str]]:
    """Two-stage: the cheap fast model screens everything; the accurate main model re-scores only
    jobs the fast model rates 55+ (the fast model runs generous, so low scores stay low)."""
    if env().score_with_fast_model:
        return await _score(job, fast=True)
    quick = await _score(job, fast=True)
    if quick[0] < SECOND_OPINION_FROM:
        return quick
    return await _score(job, fast=False)


async def _score(job: Job, fast: bool) -> tuple[int, list[str], list[str]]:
    result = await complete_json(f"""{candidate_logistics()}

JOB: {job.title} at {job.company} — {job.location} {job.salary}
{job.description[:10000]}

Return {{"score": 0-100, "reasons": ["up to 4 short reasons"], "missing": ["must-have requirements the candidate lacks"],
 "hard_blocker": "empty or e.g. 'requires US citizenship / security clearance / 10+ yrs'"}}""",
        system=SCORE_SYSTEM, max_tokens=800, context=master.prompt_context(), fast=fast)
    s = int(result.get("score", 0))
    reasons = list(result.get("reasons", []))
    if result.get("hard_blocker"):
        reasons.insert(0, f"Blocker: {result['hard_blocker']}")
        s = min(s, 40)
    return s, reasons, list(result.get("missing", []))
