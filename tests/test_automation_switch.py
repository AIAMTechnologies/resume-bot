"""Turning automation on/off live, and screening/scoring on demand."""
import asyncio
from uuid import uuid4

import httpx
import pytest

from resumebot import db
from resumebot.engine import outcomes, pipeline, scheduler, screening, stats
from resumebot.models import Job, JobStatus
from resumebot.notify import telegram
from resumebot.web.app import app


@pytest.fixture
def fake_loops(monkeypatch):
    """Replace the real workers with idle ones so tests don't discover/apply anything."""
    started = []

    def idle(name):
        async def loop(*_):
            started.append(name)
            await asyncio.Event().wait()
        return loop
    for name in ("discovery_loop", "triage_loop", "inbox_loop", "daily_summary_loop", "source_loop"):
        monkeypatch.setattr(scheduler, name, idle(name))
    monkeypatch.setattr(telegram, "poll_forever", idle("telegram"))
    async def no_send(*a, **k):
        return None
    monkeypatch.setattr(telegram, "send", no_send)
    return started


async def test_start_and_stop_automation(fake_loops):
    try:
        await scheduler.start_background(automatic=False)
        assert not scheduler.automation_running() and db.kv_get("automatic_mode") is False
        outcomes.set_global_pause(True)
        msg = await scheduler.start_automation()
        await asyncio.sleep(0)
        assert "on" in msg and scheduler.automation_running()
        assert not outcomes.global_paused()  # turning automation on lifts a pause
        assert {"discovery_loop", "triage_loop"} <= set(fake_loops)
        assert "already" in (await scheduler.start_automation()).lower()

        # A job caught mid-application is parked for review, never re-queued.
        job = db.save(Job(source="greenhouse", external_id=uuid4().hex, company="C", title="T", url="u",
                          status=JobStatus.APPLYING))
        msg = await scheduler.stop_automation()
        assert not scheduler.automation_running() and db.kv_get("automatic_mode") is False
        assert f"#{job.id}" in msg
        with db.session() as s:
            assert s.get(Job, job.id).status == JobStatus.REVIEW
        # Telegram keeps listening while automation is off.
        assert any(t.get_name() == "telegram" and not t.done() for t in scheduler.all_tasks())
    finally:
        await scheduler.shutdown()


async def test_dashboard_and_telegram_switch(fake_loops):
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        await scheduler.start_background(automatic=False)
        async with client:
            assert "Turn automation on" in (await client.get("/")).text
            r = await client.post("/control/automation", data={"on": "on"})
            assert r.status_code == 303 and scheduler.automation_running()
            page = (await client.get("/jobs")).text
            assert "Automation is on" in page and "Turn automation off" in page
            await client.post("/control/automation", data={"on": "off"})
            assert not scheduler.automation_running()
        assert "on" in (await telegram.handle_callback("am:on")).lower() and scheduler.automation_running()
        text, buttons = await telegram.handle_command("/status")
        assert "automation is on" in text.lower() and ("⏹ Turn automation off", "am:off") in buttons[0]
        text, _ = await telegram.handle_command("/automation off")
        assert not scheduler.automation_running()
    finally:
        await scheduler.shutdown()


async def test_score_now_and_backlog(monkeypatch):
    async def fake_score(job):
        return 81, ["Strong IR background"], []
    monkeypatch.setattr(pipeline.matcher, "score", fake_score)
    monkeypatch.setattr(pipeline.matcher, "prefilter", lambda job: (True, ""))
    async def no_review(*a, **k):
        return None
    monkeypatch.setattr(pipeline.review, "job_review", no_review)
    jobs = [db.save(Job(source="greenhouse", external_id=uuid4().hex, company="C", title="Security Analyst",
                        url="u", description="SOC work", status=JobStatus.NEW)) for _ in range(3)]
    assert "score 81" in await pipeline.screen_one(jobs[0].id)
    assert "Screening" in screening.start(batch=2)
    await asyncio.wait_for(screening._task, 10)
    with db.session() as s:
        assert all(s.get(Job, j.id).match_score == 81 for j in jobs)
    assert screening.waiting() == 0 and "No jobs" in screening.start()


def test_jobs_list_shows_scored_first():
    unscored = db.save(Job(source="greenhouse", external_id=uuid4().hex, company="C", title="New one", url="u",
                           status=JobStatus.NEW))
    scored = db.save(Job(source="greenhouse", external_id=uuid4().hex, company="C", title="Scored", url="u",
                         status=JobStatus.REVIEW, match_score=99))
    ids = [j.id for j in stats.jobs(limit=10_000)]
    assert ids.index(scored.id) < ids.index(unscored.id)


async def test_jobs_page_offers_score_buttons():
    job = db.save(Job(source="greenhouse", external_id=uuid4().hex, company="C", title="Waiting job", url="u",
                      status=JobStatus.NEW))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    async with client:
        page = (await client.get("/jobs?status=new")).text
    assert f"/jobs/{job.id}/score" in page and "Screen waiting jobs now" in page
