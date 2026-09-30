"""Outcomes you record by hand — one implementation shared by the dashboard and Telegram.

Job outcomes close out a job (and any pending review/Telegram items for it). Application
outcomes track what happened after submitting. The global pause stops all automatic work.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import select

from .. import db
from ..models import Application, AppStatus, Job, JobStatus, ReviewItem, utcnow


@dataclass(frozen=True)
class Outcome:
    key: str
    label: str        # button text
    emoji: str
    status: str       # resulting JobStatus
    reason: str
    log_application: bool = False  # counts as an application (dashboard totals, per-company cap)


JOB_OUTCOMES: dict[str, Outcome] = {o.key: o for o in [
    Outcome("applied", "I applied", "✅", JobStatus.APPLIED, "applied manually", log_application=True),
    Outcome("already_applied", "Applied before", "🗂", JobStatus.APPLIED, "applied before (outside the bot)",
            log_application=True),
    Outcome("unavailable", "Posting unavailable", "🚫", JobStatus.CLOSED, "posting no longer available"),
    Outcome("not_interested", "Not interested", "🙅", JobStatus.SKIPPED, "not interested"),
    Outcome("not_eligible", "Not eligible", "⛔", JobStatus.SKIPPED, "not eligible"),
    Outcome("queue", "Queue to apply", "▶️", JobStatus.QUEUED, "queued by you"),
    Outcome("rescreen", "Re-screen", "🔁", JobStatus.NEW, ""),
]}

# Shown as the quick row on Telegram cards and dashboard rows.
QUICK_JOB_OUTCOMES = ("applied", "unavailable", "not_interested")

APP_OUTCOMES: dict[str, tuple[str, str]] = {
    AppStatus.CONFIRMED: ("Confirmed", "📨"),
    AppStatus.ASSESSMENT: ("Assessment", "📝"),
    AppStatus.INTERVIEW: ("Interview", "🎤"),
    AppStatus.OFFER: ("Offer", "🎉"),
    AppStatus.REJECTED: ("Rejected", "❌"),
    AppStatus.GHOSTED: ("Ghosted", "👻"),
    AppStatus.WITHDRAWN: ("Withdrawn", "↩️"),
}


class OutcomeError(ValueError):
    status_code = 409


class NotFound(OutcomeError):
    status_code = 404


class BadRequest(OutcomeError):
    status_code = 400


def record_job(job_id: int, key: str, via: str = "dashboard") -> str:
    """Apply a job outcome. Returns a short confirmation for the UI."""
    from . import actions
    outcome = JOB_OUTCOMES.get(key)
    if outcome is None:
        raise BadRequest("Unknown outcome.")
    with db.session() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise NotFound("Job not found.")
        if actions.busy(job_id) or job.status == JobStatus.APPLYING:
            raise OutcomeError("This job is being worked on right now — try again in a minute.")
        if job.status == JobStatus.APPLIED:
            if key in ("applied", "already_applied"):
                return f"Already logged as applied: {job.title} @ {job.company}"
            raise OutcomeError("Already submitted — update the application's status instead "
                               "(Rejected, Withdrawn, …).")
        job.status = outcome.status
        job.status_reason = f"{outcome.reason} ({via})" if outcome.reason else ""
        if key == "rescreen":
            job.match_score, job.match_reasons, job.missing_skills = None, [], []
        job.updated_at = utcnow()
        s.add(job)
        already_logged = s.exec(select(Application).where(Application.job_id == job_id,
                                                          Application.submitted_at.is_not(None))).first()
        if outcome.log_application and not already_logged:
            s.add(Application(job_id=job.id, source=job.source, company=job.company, title=job.title,
                              submitted_at=utcnow(), status=AppStatus.SUBMITTED,
                              error="manual" if key == "applied" else "applied before the bot"))
        # Close out anything still waiting on you for this job.
        done = "done" if outcome.log_application else ("approved" if key == "queue" else "skipped")
        if key != "rescreen":
            for item in s.exec(select(ReviewItem).where(ReviewItem.job_id == job_id,
                                                        ReviewItem.status == "pending")):
                item.status, item.resolved_at = done, utcnow()
                s.add(item)
        s.commit()
        title, company = job.title, job.company
    message = f"{outcome.emoji} {outcome.label}: {title} @ {company}"
    db.log(f"{message} (via {via})", kind="control", job_id=job_id)
    return message


def record_application(app_id: int, status: str, via: str = "dashboard") -> str:
    if status not in APP_OUTCOMES and status != AppStatus.SUBMITTED:
        raise BadRequest("Unknown application status.")
    with db.session() as s:
        app = s.get(Application, app_id)
        if app is None:
            raise NotFound("Application not found.")
        app.status = status
        if status != AppStatus.SUBMITTED:
            app.last_response_at = utcnow()
            if not app.response_summary:
                app.response_summary = f"marked {status} by you"
        s.add(app)
        s.commit()
        company, title, job_id = app.company, app.title, app.job_id
    label, emoji = APP_OUTCOMES.get(status, ("Submitted", "📤"))
    message = f"{emoji} {label}: {title} @ {company}"
    db.log(f"{message} (via {via})", kind="control", job_id=job_id)
    return message


def global_paused() -> bool:
    return bool(db.kv_get("global_pause", False))


def set_global_pause(paused: bool, via: str = "dashboard") -> str:
    db.kv_set("global_pause", bool(paused))
    message = ("⏸ Everything paused — no discovery, screening, or applications until you resume."
               if paused else "▶️ Automation resumed.")
    db.log(f"{message} (via {via})", kind="control", level="warning" if paused else "success")
    return message
