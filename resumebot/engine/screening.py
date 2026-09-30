"""Screen the backlog of new jobs on demand (works with automation on or off)."""
from __future__ import annotations

import asyncio

from sqlmodel import func, select

from .. import db
from ..models import Job, JobStatus
from . import pipeline

_task: asyncio.Task | None = None
_progress: dict = {}


def waiting() -> int:
    with db.session() as s:
        return s.exec(select(func.count()).select_from(Job).where(Job.status == JobStatus.NEW)).one()


def running() -> bool:
    return _task is not None and not _task.done()


def status() -> dict:
    return {"running": running(), "waiting": waiting(), **_progress}


def start(batch: int = 25, via: str = "dashboard") -> str:
    """Screen everything waiting, in batches. Rule filters first; only survivors use the AI."""
    global _task
    if running():
        return f"Already screening — {waiting()} job(s) left."
    total = waiting()
    if not total:
        return "No jobs are waiting for screening."
    _progress.clear()
    _progress.update(total=total, done=0)

    async def run():
        try:
            while waiting():
                before = waiting()
                await pipeline.triage_new(limit=batch)
                _progress["done"] = total - waiting()
                if waiting() >= before:  # nothing moved (e.g. AI unavailable) — stop rather than spin
                    db.log("Screening stopped: no progress (is Claude reachable?)", level="warning", kind="score")
                    break
            db.log(f"Screening finished: {_progress.get('done', 0)} job(s) screened", kind="score", level="success")
        except Exception as error:  # noqa: BLE001
            db.log(f"Screening failed: {error}", level="error", kind="score")

    _task = asyncio.create_task(run(), name="screen-backlog")
    db.log(f"Screening {total} waiting job(s) (via {via})", kind="score")
    return f"Screening {total} waiting job(s). Rule filters run first; only matches use Claude."
