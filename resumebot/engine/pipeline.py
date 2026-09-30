"""The job pipeline: discover → filter → score → tailor → apply → record."""
from __future__ import annotations

import random
import time
from pathlib import Path

from sqlalchemy import case
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from .. import db
from ..browser import guards
from ..browser.human import Human
from ..browser.session import browsers
from ..config import DATA_DIR, answers, settings
from ..models import Application, AppStatus, Job, JobStatus, ReviewItem, utcnow
from ..notify import telegram
from ..notify.telegram import esc
from ..profile.ingest import target_titles
from ..resume import tailor as tailoring
from ..sources import SOURCES
from ..sources.base import ApplyContext, JobData, ManualRequired, Materials, NeedsInput
from ..sources.routing import Rerouted, route
from . import matcher, review
from .pacing import Gate
from .questions import Answerer


# Sources where the bot prepares everything and you click submit (never automated in a browser).
ASSIST_SOURCES = {"linkedin"}


# ---------------- discovery ----------------

def upsert(jobs: list[JobData]) -> int:
    new = 0
    with db.session() as s:
        for jd in jobs:
            if not jd.title or not jd.external_id:
                continue
            exists = s.exec(select(Job.id).where(Job.source == jd.source, Job.external_id == jd.external_id)).first()
            if exists:
                continue
            s.add(Job(**jd.__dict__, dedupe_key=matcher.dedupe_key(jd.company, jd.title)))
            try:
                s.commit()
                new += 1
            except IntegrityError:
                s.rollback()
    return new


async def discover(source_name: str) -> int:
    src = SOURCES[source_name]
    titles = target_titles()
    started = time.monotonic()
    try:
        found = await src.discover(titles)
    except guards.ChallengeDetected as e:
        await handle_challenge(source_name, e)
        return 0
    except guards.NotLoggedIn:
        await handle_logged_out(source_name)
        return 0
    new = upsert(found)
    with db.session() as s:
        from ..models import SourceState
        st = s.get(SourceState, source_name) or SourceState(source=source_name)
        st.last_discover_at = utcnow()
        s.add(st)
        s.commit()
    db.log(f"Discovered {len(found)} jobs ({new} new) in {time.monotonic() - started:.0f}s",
           source=source_name, kind="discover", level="success" if new else "info")
    return new


# ---------------- filtering & scoring ----------------

async def triage_new(limit: int = 25) -> None:
    cfg = settings().matching
    keywords = settings().targets.title_keywords
    priority = case(*[(Job.title.ilike(f"%{word}%"), rank) for rank, word in enumerate(keywords)],
                    else_=len(keywords)) if keywords else Job.discovered_at
    regions = settings().targets.locations.preferred_regions
    regional_priority = case(*[(Job.location.ilike(f"%{region}%"), 0) for region in regions],
                             else_=1) if regions else priority
    with db.session() as s:
        new_jobs = list(s.exec(select(Job).where(Job.status == JobStatus.NEW)
                              .order_by(priority, regional_priority, Job.discovered_at).limit(limit)))
    from . import runtime
    for index, job in enumerate(new_jobs, 1):
        runtime.update("triage", message=f"Checking {index}/{len(new_jobs)}: #{job.id} {job.title} @ {job.company}", job_id=job.id)
        ok, why = matcher.prefilter(job)
        if not ok:
            job.status, job.status_reason = JobStatus.SKIPPED, why
            db.save(job)
            continue
        if not job.description:
            job.status, job.status_reason = JobStatus.SKIPPED, "no description"
            db.save(job)
            continue
        try:
            runtime.update("triage", message=f"Scoring {index}/{len(new_jobs)}: #{job.id} {job.title} @ {job.company}")
            job.match_score, job.match_reasons, job.missing_skills = await matcher.score(job)
        except Exception as e:  # noqa: BLE001
            db.log(f"Scoring failed for #{job.id}: {e}", level="error", kind="score", job_id=job.id)
            continue
        if job.match_score >= cfg.auto_apply_score:
            job.status, job.status_reason = JobStatus.QUEUED, f"score {job.match_score}"
        elif job.match_score >= cfg.review_score:
            job.status, job.status_reason = JobStatus.REVIEW, f"borderline score {job.match_score}"
        else:
            job.status, job.status_reason = JobStatus.SKIPPED, f"low score {job.match_score}"
        job.updated_at = utcnow()
        db.save(job)
        if job.status == JobStatus.REVIEW:
            await review.job_review(job, job.status_reason)
        db.log(f"Scored {job.match_score}: {job.title} @ {job.company} → {job.status}",
               source=job.source, kind="score", job_id=job.id)


# ---------------- applying ----------------

def company_recent_count(company: str, days: int = 90) -> int:
    """Applications submitted or prepared for this company recently (normalized name).

    Manual cards count only while pending: once you tap "I applied" they become an Application.
    """
    from datetime import timedelta
    key = matcher.norm(company)
    since = utcnow() - timedelta(days=days)
    with db.session() as s:
        apps = s.exec(select(Application.company).where(Application.submitted_at >= since)).all()
        cards = s.exec(select(Job.company).join(ReviewItem, ReviewItem.job_id == Job.id).where(
            ReviewItem.kind == "manual", ReviewItem.created_at >= since, ReviewItem.status == "pending")).all()
    return sum(1 for c in [*apps, *cards] if matcher.norm(c) == key)


def next_job(source_name: str) -> Job | None:
    cap = settings().matching.max_apps_per_company_90d
    with db.session() as s:
        candidates = list(s.exec(select(Job).where(Job.source == source_name, Job.status == JobStatus.QUEUED)
                                 .order_by(Job.match_score.desc(), Job.discovered_at).limit(25)))
    for job in candidates:
        if cap and company_recent_count(job.company) >= cap:
            job.status, job.status_reason = JobStatus.SKIPPED, f"company cap ({cap} per 90 days) reached"
            db.save(job)
            db.log(f"Skipped {job.title} @ {job.company}: already applied {cap}x in 90 days",
                   source=source_name, kind="apply", job_id=job.id)
            continue
        return job
    return None


async def prepare(job: Job) -> tuple[Materials, tailoring.TailoredResume]:
    contact = answers().get("contact", {})
    tr = await tailoring.tailor_with_retry(job, contact)
    cover = ""
    cover_pdf = None
    if settings().ats.include_cover_letter in ("always", "when_optional"):
        cover = await tailoring.cover_letter(job, contact)
        cover_pdf = _cover_pdf(cover, contact, tr.pdf.parent)
    db.log(f"Tailored resume: ATS {tr.report.score}, keywords {tr.report.keyword_coverage:.0%}"
           + (f", dropped {len(tr.dropped)} unsupported claims" if tr.dropped else ""),
           source=job.source, kind="tailor", job_id=job.id)
    return Materials(tr.pdf, tr.docx, cover, cover_pdf), tr


def _cover_pdf(text: str, contact: dict, folder: Path) -> Path:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    from xml.sax.saxutils import escape

    path = folder / "Cover_Letter.pdf"
    style = getSampleStyleSheet()["Normal"]
    name = " ".join(x for x in [contact.get("first_name"), contact.get("last_name")] if x)
    story = [Paragraph(escape(name), style), Paragraph(escape(contact.get("email", "")), style), Spacer(1, 16)]
    for para in text.split("\n\n"):
        story += [Paragraph(escape(para.strip()), style), Spacer(1, 8)]
    story.append(Paragraph(escape(name), style))
    SimpleDocTemplate(str(path), pagesize=LETTER, leftMargin=inch, rightMargin=inch).build(story)
    return path


async def _screenshot(page, job: Job, full_page: bool = False) -> str:
    path = DATA_DIR / "screenshots" / f"{job.id}_{int(time.time())}.png"
    try:
        await page.screenshot(path=str(path), full_page=full_page)
        return str(path)
    except Exception:
        return ""


async def handle_challenge(source_name: str, e: guards.ChallengeDetected, job: Job | None = None) -> None:
    until = Gate(source_name).cooldown(f"{e.kind}")
    db.log(f"⚠️ {e.kind} — pausing {source_name} until {until:%Y-%m-%d %H:%M} UTC", level="error",
           source=source_name, kind="challenge", job_id=job.id if job else None)
    await telegram.send(f"⚠️ <b>{esc(source_name)}</b> hit a <b>{esc(e.kind)}</b>.\nPaused for "
                        f"{Gate(source_name).cfg.challenge_cooldown_hours}h. Nothing was solved automatically.\n"
                        f"{esc(e.url)}")
    if job:
        job.status, job.status_reason = JobStatus.MANUAL, f"{e.kind}"
        db.save(job)
        await review.manual_review(job, f"{e.kind} during application")
    await browsers.close()


async def handle_logged_out(source_name: str) -> None:
    Gate.set_paused(source_name, True, "logged out — run: resumebot login " + source_name)
    db.log(f"{source_name} session is logged out", level="error", source=source_name, kind="system")
    await telegram.send(f"🔑 <b>{esc(source_name)}</b> is logged out. On the Mac run:\n"
                        f"<code>resumebot login {esc(source_name)}</code>\nthen /resume {esc(source_name)}")
    await browsers.close()


def _reroute(job: Job, url: str) -> Job | None:
    r = route(url)
    if r.source in ("greenhouse", "lever", "ashby") and r.slug and r.job_id:
        target, status = r.source, JobStatus.QUEUED
        apply_url = (f"https://job-boards.greenhouse.io/embed/job_app?for={r.slug}&token={r.job_id}"
                     if r.source == "greenhouse" else url)
    else:
        # Workday, company career sites, embedded boards: prepared by the bot, applied by you.
        target, status, apply_url = ("workday" if r.source == "workday" else "external"), JobStatus.MANUAL, url
    new = Job(source=target, external_id=f"{r.slug}:{r.job_id}" if r.job_id else url[:300],
              company=job.company, title=job.title, location=job.location, remote=job.remote,
              url=url, apply_url=apply_url, description=job.description, salary=job.salary,
              match_score=job.match_score, match_reasons=job.match_reasons, status=status,
              status_reason=f"rerouted from {job.source}", dedupe_key=job.dedupe_key)
    with db.session() as s:
        exists = s.exec(select(Job).where(Job.source == new.source, Job.external_id == new.external_id)).first()
        if exists:
            return None
    return db.save(new)


async def apply_job(job: Job, dry_run: bool = False, draft_version: str | None = None) -> Application | None:
    from sqlalchemy import update
    from . import drafts
    # Claim in SQLite, so repeated clicks and scheduler races cannot submit twice.
    if draft_version is not None:
        drafts.load(job.id, draft_version)
    with db.session() as s:
        claimed = s.execute(update(Job).where(Job.id == job.id,
            Job.status.notin_([JobStatus.APPLYING, JobStatus.APPLIED])).values(
                status=JobStatus.APPLYING, updated_at=utcnow()))
        s.commit()
        if not claimed.rowcount:
            raise ValueError('This job is already applying or has been submitted.')
    try:
        return await _apply_job(job, dry_run, draft_version)
    except BaseException:
        with db.session() as s:
            current = s.get(Job, job.id)
            if current and current.status == JobStatus.APPLYING:
                current.status, current.status_reason = JobStatus.REVIEW, 'Application interrupted; check before retrying'
                s.add(current)
                s.commit()
        raise


async def _apply_job(job: Job, dry_run: bool = False, draft_version: str | None = None) -> Application | None:
    from . import drafts
    src = SOURCES[job.source]
    prior_status, prior_reason = job.status, job.status_reason
    job.status, job.updated_at = JobStatus.APPLYING, utcnow()
    db.save(job)
    try:
        materials, tr = drafts.load(job.id, draft_version) if draft_version else await drafts.prepare(job)
    except Exception as e:  # noqa: BLE001
        job.status, job.status_reason = (prior_status, prior_reason) if dry_run else (
            JobStatus.FAILED, f"tailoring failed: {e}"[:300])
        db.save(job)
        db.log(f"Tailoring failed: {e}", level="error", source=job.source, kind="tailor", job_id=job.id)
        return None

    if job.source in ASSIST_SOURCES or job.source in ("workday", "external"):
        # Prepared for you, submitted by you — no browser, no LinkedIn session involved.
        if dry_run:
            job.status, job.status_reason, job.updated_at = prior_status, prior_reason, utcnow()
            db.save(job)
            db.log(f"Dry run: materials prepared for {job.title} @ {job.company} (ATS {tr.report.score}); "
                   "nothing sent", source=job.source, kind="apply", job_id=job.id)
            return None
        outreach = ""
        if job.source == "linkedin":
            try:
                outreach = await tailoring.outreach_note(job)
            except Exception:  # noqa: BLE001
                outreach = ""
        job.status, job.status_reason = JobStatus.MANUAL, "ready for you to submit"
        job.updated_at = utcnow()
        db.save(job)
        await review.manual_review(job, "LinkedIn Easy Apply" if job.source == "linkedin" and job.easy_apply
                                   else f"{job.source} site",
                                   str(materials.resume_pdf), materials.cover_letter, outreach)
        db.log(f"Prepared for you: {job.title} @ {job.company} (ATS {tr.report.score})",
               level="success", source=job.source, kind="apply", job_id=job.id)
        return None

    app = Application(job_id=job.id, source=job.source, company=job.company, title=job.title,
                      resume_pdf=str(materials.resume_pdf), resume_docx=str(materials.resume_docx),
                      cover_letter=materials.cover_letter, ats_score=tr.report.score,
                      keyword_coverage=tr.report.keyword_coverage, status=AppStatus.FAILED)
    started = time.monotonic()
    answerer = Answerer(job.title, job.company, job.description, job.location)
    try:
        async with browsers.page(job.source) as page:
            human = Human(page)
            ctx = ApplyContext(job=job, page=page, human=human, materials=materials, answer=answerer, dry_run=dry_run)
            try:
                result = await src.apply(ctx)
            finally:
                app.screenshot = await _screenshot(ctx.page, job, full_page=dry_run)
        app.answers = {**answerer.log, **{k: {"answer": v} for k, v in result.extra.get("answers", {}).items()
                                          if k not in answerer.log}}
        app.duration_seconds = round(time.monotonic() - started, 1)
        if result.submitted:
            app.status, app.submitted_at = AppStatus.SUBMITTED, utcnow()
            job.status, job.status_reason = JobStatus.APPLIED, result.note
            db.log(f"✅ Applied: {job.title} @ {job.company} ({app.duration_seconds:.0f}s, ATS {app.ats_score})",
                   level="success", source=job.source, kind="apply", job_id=job.id)
            telegram.notify(f"✅ Applied: <b>{esc(job.title)}</b> — {esc(job.company)} ({job.source}, "
                            f"match {job.match_score}, ATS {app.ats_score})")
        else:
            app.error = result.note
            if dry_run:
                # Never drop a dry-run job straight back into the automatic queue: a queued job
                # would then be submitted without anyone approving what the dry run showed.
                if prior_status == JobStatus.QUEUED:
                    job.status, job.status_reason = JobStatus.REVIEW, "dry run done — approve to apply"
                else:
                    job.status, job.status_reason = prior_status, prior_reason
            else:
                job.status, job.status_reason = JobStatus.FAILED, result.note
            db.log(f"Not submitted: {job.title} @ {job.company} — {result.note}",
                   level="info" if dry_run else "warning", source=job.source, kind="apply", job_id=job.id)
    except NeedsInput as ni:
        job.status, job.status_reason = JobStatus.REVIEW, f"{len(ni.questions)} question(s) need you"
        app = None
        await review.question_reviews(job, ni.questions)
        db.log(f"Waiting on {len(ni.questions)} answer(s): {job.title} @ {job.company}", level="warning",
               source=job.source, kind="apply", job_id=job.id)
    except Rerouted as rr:
        new = _reroute(job, rr.url)
        job.status, job.status_reason = JobStatus.SKIPPED, f"external apply → {route(rr.url).source}"
        if new and new.status == JobStatus.MANUAL:
            await review.manual_review(new, f"applies on {new.source} site", str(materials.resume_pdf),
                                       materials.cover_letter)
        db.log(f"Rerouted {job.title} @ {job.company} to {route(rr.url).source}" + ("" if new else " (already known)"),
               source=job.source, kind="apply", job_id=job.id)
        app = None
    except ManualRequired as mr:
        job.status, job.status_reason = JobStatus.MANUAL, str(mr)
        await review.manual_review(job, str(mr), str(materials.resume_pdf), materials.cover_letter)
        app = None
    except guards.ChallengeDetected as e:
        await handle_challenge(job.source, e, job)
        app = None
    except guards.NotLoggedIn:
        job.status = JobStatus.QUEUED
        await handle_logged_out(job.source)
        app = None
    except Exception as e:  # noqa: BLE001
        job.status, job.status_reason = JobStatus.FAILED, f"{type(e).__name__}: {e}"[:300]
        app.error = job.status_reason
        db.log(f"Apply error: {job.status_reason}", level="error", source=job.source, kind="apply", job_id=job.id)
    job.updated_at = utcnow()
    db.save(job)
    if app is not None and not dry_run:
        db.save(app)
    return app


async def tick_source(source_name: str) -> str:
    """One scheduler step for a source. Returns what it did (for logs/tests)."""
    gate = Gate(source_name)
    gate.clear_expired()
    decision = gate.check()
    if db.kv_get("run_now") and decision.reason == "waiting (human gap)":
        db.kv_set("run_now", False)
        decision.ok = True
    if not decision.ok:
        return f"wait: {decision.reason}"
    job = next_job(source_name)
    if job is None:
        return "idle: nothing queued"
    src = SOURCES[source_name]
    cfg = gate.cfg
    if cfg.mode != "assist" and cfg.browse_only_ratio and random.random() < cfg.browse_only_ratio:
        # Decoy: look at a job without applying, like a person comparing options.
        with db.session() as s:
            decoy = s.exec(select(Job).where(Job.source == source_name, Job.status == JobStatus.SKIPPED)
                           .order_by(Job.discovered_at.desc()).limit(20)).all()
        if decoy:
            d = random.choice(decoy)
            try:
                async with browsers.page(source_name) as page:
                    await src.browse_only(page, Human(page), d)
            except guards.ChallengeDetected as e:
                await handle_challenge(source_name, e)
                return "challenge"
            gate.record_action()
            return f"browsed #{d.id}"
    from . import runtime
    runtime.update(f"apply:{source_name}", message=f"Preparing and applying: #{job.id} {job.title} @ {job.company}", job_id=job.id)
    await apply_job(job)
    gate.record_action()
    return f"#{job.id}: {job.status} — {job.status_reason}"
