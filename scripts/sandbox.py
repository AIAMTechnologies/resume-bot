"""Isolated test sandboxes for dry-run testing (used by parallel test agents).

Every command needs RESUMEBOT_DATA_DIR pointing at a sandbox, so nothing touches the real DB,
browser profile, or queue. Dry runs NEVER submit.

    python scripts/sandbox.py init NAME                   # prints the env line to use
    RESUMEBOT_DATA_DIR=... python scripts/sandbox.py boards SOURCE SLUG [FILTER]
    RESUMEBOT_DATA_DIR=... python scripts/sandbox.py add SOURCE SLUG JOB_ID_OR_TITLE
    RESUMEBOT_DATA_DIR=... python scripts/sandbox.py dryrun JOB_ID
    RESUMEBOT_DATA_DIR=... python scripts/sandbox.py report JOB_ID
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REAL_DB = ROOT / "data" / "resumebot.db"
SANDBOX_ROOT = Path(os.environ.get("RESUMEBOT_SANDBOX_ROOT", "/tmp/resumebot-sandboxes"))
BLOCKED_COMPANIES = {"cohere"}  # user asked: do not run Cohere


def init(name: str) -> None:
    d = SANDBOX_ROOT / name
    (d / "screenshots").mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(REAL_DB)
    dst = sqlite3.connect(d / "resumebot.db")
    src.backup(dst)
    # Start clean: keep the master profile, drop real jobs/apps/reviews/events.
    for table in ("application", "reviewitem", "event", "job", "sourcestate"):
        dst.execute(f"DELETE FROM {table}")
    dst.commit()
    dst.close()
    src.close()
    print(f"export RESUMEBOT_DATA_DIR={d} RESUMEBOT_HEADLESS=1 TELEGRAM_BOT_TOKEN= TELEGRAM_CHAT_ID=")


def _guard() -> None:
    data = os.environ.get("RESUMEBOT_DATA_DIR", "")
    if not data or Path(data).resolve() == (ROOT / "data").resolve():
        sys.exit("Refusing: set RESUMEBOT_DATA_DIR to a sandbox (python scripts/sandbox.py init NAME)")
    if os.environ.get("TELEGRAM_BOT_TOKEN", "x"):
        os.environ["TELEGRAM_BOT_TOKEN"] = ""  # never message the user from a sandbox


async def _boards(source: str, slug: str, flt: str = "") -> None:
    import httpx

    from resumebot.sources import SOURCES
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        jobs = await SOURCES[source]._fetch_board(c, slug)
    for j in jobs:
        if flt.lower() in (j.title + " " + j.location).lower():
            print(f"{j.external_id}\t{j.title}\t{j.location}\t{j.url}")


async def _add(source: str, slug: str, key: str) -> None:
    import httpx

    from resumebot import db
    from resumebot.engine import matcher, pipeline
    from resumebot.models import Job, JobStatus
    from resumebot.sources import SOURCES
    from sqlmodel import select
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        jobs = await SOURCES[source]._fetch_board(c, slug)
    match = [j for j in jobs if j.external_id == key or j.external_id.endswith(":" + key)] or \
            [j for j in jobs if key.lower() in j.title.lower()]
    if not match:
        sys.exit(f"No job matching {key!r} on {source}/{slug}")
    jd = match[0]
    if jd.company.lower() in BLOCKED_COMPANIES or slug.lower() in BLOCKED_COMPANIES:
        sys.exit("Blocked company (user instruction)")
    pipeline.upsert([jd])
    with db.session() as s:
        job = s.exec(select(Job).where(Job.source == source, Job.external_id == jd.external_id)).one()
    job.match_score, job.match_reasons, job.missing_skills = await matcher.score(job)
    job.status = JobStatus.QUEUED
    db.save(job)
    print(f"job_id={job.id} score={job.match_score} | {job.title} @ {job.company} | {job.apply_url or job.url}")


async def _dryrun(job_id: int) -> None:
    from resumebot import db
    from resumebot.browser.session import browsers
    from resumebot.engine import pipeline
    from resumebot.models import Job
    with db.session() as s:
        job = s.get(Job, job_id)
    if job.company.lower() in BLOCKED_COMPANIES:
        sys.exit("Blocked company (user instruction)")
    try:
        await pipeline.apply_job(job, dry_run=True)
    finally:
        await browsers.shutdown()
    _report(job_id)


def _report(job_id: int) -> None:
    import json

    from resumebot import db
    from resumebot.models import Event, Job, ReviewItem
    from sqlmodel import select
    with db.session() as s:
        job = s.get(Job, job_id)
        events = [f"{e.level}: {e.message}" for e in s.exec(select(Event).where(Event.job_id == job_id).order_by(Event.ts))]
        reviews = [{"kind": r.kind, "question": r.question, "proposed": r.proposed_answer, "options": r.context}
                   for r in s.exec(select(ReviewItem).where(ReviewItem.job_id == job_id))]
    data_dir = Path(os.environ["RESUMEBOT_DATA_DIR"])
    shots = sorted((data_dir / "screenshots").glob(f"{job_id}_*.png"))
    print(json.dumps({"job": f"{job.title} @ {job.company}", "status": job.status, "reason": job.status_reason,
                      "events": events, "review_items": reviews,
                      "screenshots": [str(p) for p in shots],
                      "resume_dir": str(data_dir / "resumes")}, indent=1))


def main() -> None:
    import asyncio
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "init":
        return init(args[0])
    _guard()
    if cmd == "boards":
        asyncio.run(_boards(*args))
    elif cmd == "add":
        asyncio.run(_add(*args))
    elif cmd == "dryrun":
        asyncio.run(_dryrun(int(args[0])))
    elif cmd == "report":
        _report(int(args[0]))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
