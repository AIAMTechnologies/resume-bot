"""The review queue: borderline jobs, unanswerable questions, and manual applications."""
from __future__ import annotations

from sqlmodel import select

from .. import db
from ..models import Application, AppStatus, Job, JobStatus, ReviewItem, utcnow
from ..notify import telegram
from ..notify.telegram import esc
from .questions import remember


def _job_line(job: Job) -> str:
    return (f"<b>{esc(job.title)}</b> — {esc(job.company)}\n{esc(job.location)} · {job.source}"
            + (f" · score {job.match_score}" if job.match_score is not None else "")
            + f"\n{esc(job.url)}")


async def job_review(job: Job, reason: str) -> None:
    item = db.save(ReviewItem(job_id=job.id, kind="job", context=reason))
    reasons = "\n".join(f"• {esc(r)}" for r in job.match_reasons[:4])
    item.telegram_message_id = await telegram.send(
        f"🤔 <b>Review</b> #{item.id}: {esc(reason)}\n{_job_line(job)}\n{reasons}",
        [[("✅ Apply", f"rv:approve:{item.id}"), ("⏭ Skip", f"rv:skip:{item.id}")]])
    db.save(item)


async def question_reviews(job: Job, questions: list[tuple[str, str, list[str]]]) -> None:
    for question, proposed, options in questions:
        with db.session() as s:
            dup = s.exec(select(ReviewItem).where(ReviewItem.job_id == job.id, ReviewItem.kind == "question",
                                                  ReviewItem.question == question,
                                                  ReviewItem.status == "pending")).first()
        if dup:
            continue
        item = db.save(ReviewItem(job_id=job.id, kind="question", question=question, proposed_answer=proposed,
                                  context=" | ".join(options)))
        buttons = [[(f"Use: {proposed[:30]}", f"rv:useproposed:{item.id}")]] if proposed else []
        buttons.append([("⏭ Skip job", f"rv:skip:{item.id}")])
        item.telegram_message_id = await telegram.send(
            f"❓ <b>Question</b> #{item.id} for {esc(job.title)} — {esc(job.company)}\n\n<b>{esc(question)}</b>"
            + (f"\nOptions: {esc(', '.join(options))}" if options else "")
            + (f"\nSuggested: <i>{esc(proposed)}</i>" if proposed else "")
            + "\n\n↩️ <i>Reply to this message with your answer.</i> It'll be remembered for next time.",
            buttons)
        db.save(item)


async def manual_review(job: Job, reason: str, resume_pdf: str = "", cover_letter: str = "",
                        outreach: str = "") -> None:
    """A job you apply to yourself (LinkedIn Easy Apply, Workday, company sites) with everything prepared."""
    item = db.save(ReviewItem(job_id=job.id, kind="manual", context=reason, proposed_answer=outreach))
    linkedin = job.source == "linkedin" and job.easy_apply
    head = "💼 <b>LinkedIn Easy Apply ready</b>" if linkedin else "🖐 <b>Apply yourself</b>"
    if resume_pdf:
        await telegram.send_document(resume_pdf, f"📄 Tailored resume — {esc(job.title)} @ {esc(job.company)}")
    reasons = "\n".join(f"• {esc(r)}" for r in job.match_reasons[:3])
    body = f"{head} #{item.id}\n{_job_line(job)}\n{reasons}"
    if cover_letter:
        body += f"\n\n<b>Cover letter</b> (paste if asked):\n<i>{esc(cover_letter[:1800])}</i>"
    if outreach:
        body += f"\n\n<b>Note to the hiring manager</b> (send after applying):\n<code>{esc(outreach)}</code>"
    body += "\n\n" + ("Tap Easy Apply, attach the PDF above, submit — then tap ✅." if linkedin
                        else f"Reason: {esc(reason)}")
    item.telegram_message_id = await telegram.send(
        body, [[("Open job", job.apply_url or job.url)], [("✅ I applied", f"rv:done:{item.id}"),
                                                          ("⏭ Skip", f"rv:skip:{item.id}")]])
    db.save(item)


def _requeue_if_ready(s, job: Job) -> None:
    pending = s.exec(select(ReviewItem).where(ReviewItem.job_id == job.id, ReviewItem.kind == "question",
                                              ReviewItem.status == "pending")).first()
    if not pending:
        job.status, job.status_reason = JobStatus.QUEUED, "questions answered"
        s.add(job)


def resolve(item_id: int, action: str, answer: str = "") -> str:
    """action: approve | skip | answer | useproposed | done"""
    with db.session() as s:
        item = s.get(ReviewItem, item_id)
        if not item or item.status != "pending":
            return "Already handled."
        job = s.get(Job, item.job_id) if item.job_id else None
        item.resolved_at = utcnow()
        if action == "approve" and job:
            item.status = "approved"
            job.status, job.status_reason = JobStatus.QUEUED, "approved by you"
            s.add(job)
            msg = f"Queued: {job.title} @ {job.company}"
        elif action == "skip":
            item.status = "skipped"
            if job:
                job.status, job.status_reason = JobStatus.SKIPPED, "skipped by you"
                s.add(job)
                for other in s.exec(select(ReviewItem).where(ReviewItem.job_id == job.id,
                                                             ReviewItem.status == "pending")):
                    other.status, other.resolved_at = "skipped", utcnow()
                    s.add(other)
            msg = "Skipped."
        elif action in ("answer", "useproposed"):
            value = answer if action == "answer" else item.proposed_answer
            if not value:
                return "No answer given."
            item.status, item.answer = "answered", value
            remember(item.question, value)
            s.add(item)
            if job:
                _requeue_if_ready(s, job)
            msg = f"Saved answer for “{item.question[:60]}”"
        elif action == "done" and job:
            item.status = "done"
            job.status, job.status_reason = JobStatus.APPLIED, "applied manually"
            s.add(job)
            s.add(Application(job_id=job.id, source=job.source, company=job.company, title=job.title,
                              submitted_at=utcnow(), status=AppStatus.SUBMITTED, error="manual"))
            msg = f"Logged manual application: {job.title} @ {job.company}"
        else:
            return "Unknown action."
        s.add(item)
        s.commit()
    db.log(msg, kind="review", job_id=item.job_id)
    return msg


def answer_by_telegram_message(message_id: int, text: str) -> str:
    with db.session() as s:
        item = s.exec(select(ReviewItem).where(ReviewItem.telegram_message_id == message_id)).first()
    if not item:
        return "That message isn't a question I'm waiting on."
    if item.kind != "question":
        return "Use the buttons on that message."
    return resolve(item.id, "answer", text.strip())
