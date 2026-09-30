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


async def daily_summary_loop():
    async def run():
        from .pacing import local_now
        from . import stats
        now = local_now()
        key = f"summary_sent_{now.date()}"
        if now.hour >= 20 and not db.kv_get(key):
            s = stats.overview()
            await telegram.send(f"📊 <b>Daily summary</b>\nApplied today: {s['today']} · week: {s['week']} · "
                                f"total: {s['total']}\nResponses: {s['responses']} ({s['response_rate']}%) · "
                                f"interviews: {s['interviews']}\nReview queue: {s['pending_reviews']} · "
                                f"manual: {s['manual']}")
            db.kv_set(key, True)
    await _guard("summary", run, 600)


async def run_all() -> list[asyncio.Task]:
    tasks = [
        asyncio.create_task(discovery_loop(), name="discovery"),
        asyncio.create_task(triage_loop(), name="triage"),
        asyncio.create_task(inbox_loop(), name="inbox"),
        asyncio.create_task(telegram.poll_forever(), name="telegram"),
        asyncio.create_task(daily_summary_loop(), name="summary"),
    ]
    for name, cfg in settings().pacing.sources.items():
        if cfg.enabled and cfg.mode != "manual":
            tasks.append(asyncio.create_task(source_loop(name), name=f"apply:{name}"))
    runtime.register(tasks)
    db.log("Scheduler started", kind="system", level="success")
    await telegram.send("🤖 Resume Bot started. /status for details.")
    return tasks
