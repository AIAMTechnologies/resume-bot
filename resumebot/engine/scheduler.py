"""Long-running loops: discovery, triage, per-source apply loops, inbox, Telegram."""
from __future__ import annotations

import asyncio
import random
from datetime import timedelta

from .. import db
from ..config import env, settings
from ..models import utcnow
from ..diagnostics import redact
from ..notify import telegram
from . import pipeline, runtime
from .pacing import Gate

BROWSER_SOURCES = {"linkedin", "indeed"}


async def _guard(name: str, coro_fn, interval: float):
    """Run coro_fn forever with a pause between runs; never let one crash kill the bot."""
    while True:
        runtime.update(name, phase="running", message="Checking", started_at=utcnow(), next_at=None)
        try:
            result = await coro_fn()
            runtime.update(name, phase="waiting", message=str(result or "Check complete"), last_ok=utcnow())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            runtime.update(name, phase="error", message=redact(f"{type(e).__name__}: {e}")[:300])
            db.log(f"{name} loop error: {type(e).__name__}: {e}", level="error", kind="system")
        delay = interval * random.uniform(0.85, 1.15)
        runtime.update(name, next_at=utcnow() + timedelta(seconds=delay))
        await asyncio.sleep(delay)


async def discovery_loop():
    async def run():
        if db.kv_get("global_pause"):
            return "Global pause is on"
        for name, cfg in settings().pacing.sources.items():
            if not cfg.enabled or name in ("workday",):
                continue
            st = db.source_state(name)
            if st.paused:
                continue
            due = st.last_discover_at is None or st.last_discover_at < utcnow() - timedelta(hours=cfg.discover_every_hours)
            if not due:
                continue
            if name in BROWSER_SOURCES:
                # Browser discovery only during that source's active hours, via the same gate.
                d = Gate(name).check()
                if not d.ok and d.reason not in ("waiting (human gap)",) and "cap" not in d.reason:
                    continue
            runtime.update("discovery", message=f"Finding jobs on {name}")
            await pipeline.discover(name)
    await _guard("discovery", run, 300)


async def triage_loop():
    async def run():
        if db.kv_get("global_pause"):
            return "Global pause is on"
        return await pipeline.triage_new()
    await _guard("triage", run, 60)


async def source_loop(name: str):
    async def run():
        if db.kv_get("global_pause"):
            return "Global pause is on"
        return await pipeline.tick_source(name)
    await _guard(f"apply:{name}", run, 30)


async def inbox_loop():
    from ..inbox import gmail_api
    if not (gmail_api.configured() or (env().gmail_address and env().gmail_app_password)):
        runtime.update("inbox", phase="disabled", message="Gmail is not configured")
        return
    from ..inbox import gmail
    await _guard("inbox", gmail.check_inbox, settings().inbox.poll_minutes * 60)


async def prefetch_loop(ahead: int = 2):
    """Tailor resumes for the next queued portal jobs while a form is being filled (runs in parallel)."""
    from ..models import Job, JobStatus
    from . import drafts

    async def run():
        if db.kv_get("global_pause") or pipeline.ai_waiting():
            return "Waiting (paused or AI cooling down)"
        with db.session() as s:
            jobs = list(s.exec(db.select(Job).where(Job.status == JobStatus.QUEUED,
                                                    Job.source.in_(list(pipeline.BRISK_SOURCES)))
                               .order_by(Job.match_score.desc(), Job.discovered_at).limit(10)))
        todo = [j for j in jobs if (drafts.get(j.id) or {}).get("status") not in ("ready", "preparing")][:ahead]
        for job in todo:
            runtime.update("prefetch", message=f"Tailoring ahead: #{job.id} {job.title} @ {job.company}", job_id=job.id)
            try:
                await drafts.prepare(job)
            except Exception as error:  # noqa: BLE001
                if pipeline.ai_unavailable(error):
                    return "AI allowance exhausted"
                db.log(f"Tailoring ahead failed for #{job.id}: {error}", level="warning", kind="tailor", job_id=job.id)
        return f"{len(todo)} resume(s) prepared ahead" if todo else "Nothing to prepare"
    await _guard("prefetch", run, 30)


async def daily_summary_loop():
    async def run():
        from .pacing import local_now
        from . import health, stats
        now = local_now()
        key = f"summary_sent_{now.date()}"
        if now.hour >= 20 and not db.kv_get(key):
            s = stats.overview()
            problems = health.summary_line()
            await telegram.send(f"📊 <b>Daily summary</b>\nApplied today: {s['today']} · week: {s['week']} · "
                                f"total: {s['total']}\nResponses: {s['responses']} ({s['response_rate']}%) · "
                                f"interviews: {s['interviews']}\nReview queue: {s['pending_reviews']} · "
                                f"manual: {s['manual']}" + (f"\n{problems}" if problems else ""))
            db.kv_set(key, True)
    await _guard("summary", run, 600)


async def health_loop():
    """Every few minutes: fold new errors into recurring patterns and apply the known remedies.
    Runs whether automation is on or off, since manual actions fail too."""
    from . import health

    async def run():
        changed = health.scan()
        yours = sum(p.status == "needs-you" for p in changed)
        return (f"{len(changed)} pattern(s) updated" + (f", {yours} need you" if yours else "")) if changed else "No new errors"
    await _guard("health", run, 300)


# ---------------- automation on/off (live, no restart) ----------------

_always: list[asyncio.Task] = []       # Telegram listener: runs even when automation is off
_automation: list[asyncio.Task] = []   # discovery, screening, applying, inbox, summary


def _automation_tasks() -> list[asyncio.Task]:
    tasks = [
        asyncio.create_task(discovery_loop(), name="discovery"),
        asyncio.create_task(triage_loop(), name="triage"),
        asyncio.create_task(inbox_loop(), name="inbox"),
        asyncio.create_task(daily_summary_loop(), name="summary"),
        asyncio.create_task(prefetch_loop(), name="prefetch"),
    ]
    for name, cfg in settings().pacing.sources.items():
        if cfg.enabled and cfg.mode != "manual":
            tasks.append(asyncio.create_task(source_loop(name), name=f"apply:{name}"))
    return tasks


def automation_running() -> bool:
    return any(not t.done() for t in _automation)


def all_tasks() -> list[asyncio.Task]:
    return [*_always, *_automation]


async def start_background(automatic: bool) -> list[asyncio.Task]:
    """Called once at startup: always listen on Telegram; start automation if requested."""
    if not any(not t.done() for t in _always):
        _always[:] = [asyncio.create_task(telegram.poll_forever(), name="telegram"),
                      asyncio.create_task(health_loop(), name="health")]
    if automatic:
        await start_automation(via="startup", announce=True)
    else:
        db.kv_set("automatic_mode", False)
        runtime.register(all_tasks())
    return all_tasks()


async def start_automation(via: str = "dashboard", announce: bool = True) -> str:
    if automation_running():
        return "Automation is already running."
    _automation[:] = _automation_tasks()
    db.kv_set("automatic_mode", True)
    db.kv_set("global_pause", False)  # turning automation on means you want it working
    runtime.register(all_tasks())
    message = "🤖 Automation on — finding, screening, and applying on each source's schedule."
    db.log(f"{message} (via {via})", kind="system", level="success")
    if announce:
        await telegram.send(message + " /status for details.")
    return message


async def stop_automation(via: str = "dashboard") -> str:
    """Stop background work; the dashboard and Telegram keep running."""
    from ..browser.session import browsers
    from ..models import Job, JobStatus
    from . import actions
    tasks, _automation[:] = list(_automation), []
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    db.kv_set("automatic_mode", False)
    # A form interrupted mid-way must not be retried blindly: park it for your review.
    interrupted = []
    with db.session() as s:
        for job in s.exec(db.select(Job).where(Job.status == JobStatus.APPLYING)):
            if not actions.busy(job.id):
                job.status, job.status_reason = JobStatus.REVIEW, "interrupted when automation stopped — check before re-applying"
                s.add(job)
                interrupted.append(job.id)
        s.commit()
    if tasks and not actions._tasks:
        await browsers.close()
    runtime.register(all_tasks())
    message = "⏹ Automation off — the dashboard and Telegram still work; nothing applies automatically."
    if interrupted:
        message += f" Interrupted job(s) moved to review: {', '.join(f'#{i}' for i in interrupted)}."
    db.log(f"{message} (via {via})", kind="system", level="warning")
    return message


async def shutdown() -> None:
    tasks, _automation[:], _always[:] = all_tasks(), [], []
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def run_all() -> list[asyncio.Task]:
    """Start everything (Telegram + automation). Kept for callers and tests."""
    return await start_background(True)
