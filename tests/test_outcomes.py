"""Manual outcome buttons (dashboard + Telegram) and the global pause switch."""
from uuid import uuid4

import httpx
import pytest

from resumebot import db
from resumebot.engine import outcomes, pipeline
from resumebot.models import Application, AppStatus, Job, JobStatus, ReviewItem
from resumebot.notify import telegram
from resumebot.web.app import app


@pytest.fixture
def job():
    return db.save(Job(source="greenhouse", external_id=uuid4().hex, company=f"Co {uuid4().hex[:6]}",
                       title="Security Analyst", url="https://example.com/job", status=JobStatus.REVIEW,
                       match_score=72))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(telegram, "enabled", lambda: False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _apps(job_id):
    with db.session() as s:
        return list(s.exec(db.select(Application).where(Application.job_id == job_id)))


def _job(job_id):
    with db.session() as s:
        return s.get(Job, job_id)


def test_i_applied_logs_one_application_and_clears_review_items(job):
    item = db.save(ReviewItem(job_id=job.id, kind="manual"))
    outcomes.record_job(job.id, "applied")
    assert _job(job.id).status == JobStatus.APPLIED
    assert len(_apps(job.id)) == 1 and _apps(job.id)[0].status == AppStatus.SUBMITTED
    with db.session() as s:
        assert s.get(ReviewItem, item.id).status == "done"
    # Pressing it again (e.g. on Telegram and the dashboard) doesn't double count.
    assert "Already logged" in outcomes.record_job(job.id, "applied")
    assert len(_apps(job.id)) == 1
    assert pipeline.company_recent_count(job.company) == 1


@pytest.mark.parametrize("key,status", [("unavailable", JobStatus.CLOSED), ("not_interested", JobStatus.SKIPPED),
                                        ("not_eligible", JobStatus.SKIPPED), ("queue", JobStatus.QUEUED)])
def test_closing_outcomes(job, key, status):
    item = db.save(ReviewItem(job_id=job.id, kind="job"))
    outcomes.record_job(job.id, key, via="Telegram")
    j = _job(job.id)
    assert j.status == status and "Telegram" in j.status_reason
    assert not _apps(job.id)
    with db.session() as s:
        assert s.get(ReviewItem, item.id).status in ("skipped", "approved")


def test_rescreen_clears_score(job):
    outcomes.record_job(job.id, "rescreen")
    j = _job(job.id)
    assert j.status == JobStatus.NEW and j.match_score is None


def test_submitted_job_cannot_be_marked_unavailable(job):
    outcomes.record_job(job.id, "applied")
    with pytest.raises(outcomes.OutcomeError, match="update the application"):
        outcomes.record_job(job.id, "unavailable")


def test_unknown_inputs(job):
    with pytest.raises(outcomes.BadRequest):
        outcomes.record_job(job.id, "explode")
    with pytest.raises(outcomes.NotFound):
        outcomes.record_job(999_999, "applied")
    with pytest.raises(outcomes.BadRequest):
        outcomes.record_application(1, "garbage")


def test_application_outcomes(job):
    outcomes.record_job(job.id, "already_applied")
    app_id = _apps(job.id)[0].id
    outcomes.record_application(app_id, AppStatus.INTERVIEW)
    with db.session() as s:
        a = s.get(Application, app_id)
    assert a.status == AppStatus.INTERVIEW and a.last_response_at is not None


def test_global_pause_toggle():
    outcomes.set_global_pause(True)
    assert outcomes.global_paused()
    outcomes.set_global_pause(False)
    assert not outcomes.global_paused()


async def test_dashboard_buttons(client, job):
    async with client:
        page = await client.get("/jobs")
        assert f"/jobs/{job.id}/outcome/applied" in page.text and f"/jobs/{job.id}/outcome/unavailable" in page.text
        assert "Turn automation on" in page.text  # automation is off in tests
        r = await client.post(f"/jobs/{job.id}/outcome/unavailable", headers={"referer": "http://test/jobs?status=review"})
        assert r.status_code == 303 and r.headers["location"].startswith("/jobs?status=review&msg=")
        outcome_page = r.headers["location"]
        assert _job(job.id).status == JobStatus.CLOSED
        assert (await client.post(f"/jobs/{job.id}/outcome/nope")).status_code == 400
        assert (await client.post("/jobs/999999/outcome/applied")).status_code == 404

        r = await client.post("/control/global-pause", data={"paused": "on"})
        assert r.status_code == 303 and outcomes.global_paused()
        assert outcomes.global_paused()
        await client.post("/control/global-pause", data={"paused": "off"})
        assert not outcomes.global_paused()

        # Message shows after redirect; errors show as alerts.
        assert "posting unavailable" in (await client.get(outcome_page)).text.lower()


async def test_application_buttons(client, job):
    outcomes.record_job(job.id, "applied")
    app_id = _apps(job.id)[0].id
    async with client:
        detail = await client.get(f"/applications/{app_id}")
        assert "Interview" in detail.text and "Withdrawn" in detail.text
        assert "Update ▾" in (await client.get("/applications")).text
        assert (await client.post(f"/applications/{app_id}/status", data={"status": "rejected"})).status_code == 303
    with db.session() as s:
        assert s.get(Application, app_id).status == AppStatus.REJECTED


async def test_telegram_buttons_and_commands(job):
    assert await telegram.handle_callback(f"jo:not_interested:{job.id}")
    assert _job(job.id).status == JobStatus.SKIPPED
    outcomes.record_job(job.id, "applied")
    app_id = _apps(job.id)[0].id
    assert "Offer" in await telegram.handle_callback(f"ao:offer:{app_id}")
    assert "paused" in (await telegram.handle_callback("gp:on")).lower() and outcomes.global_paused()
    text, buttons = await telegram.handle_command("/status")
    assert "automation is off" in text.lower() and buttons == [[("🤖 Turn automation on", "am:on")]]
    await telegram.handle_callback("gp:off")
    text, buttons = await telegram.handle_command(f"/job {job.id}")
    assert f"#{job.id}" in text
    text, buttons = await telegram.handle_command(f"/app {app_id}")
    assert any(d == f"ao:interview:{app_id}" for row in buttons for _, d in row)
    assert "Unknown outcome" in await telegram.handle_callback(f"jo:explode:{job.id}")
    # Every callback payload fits Telegram's 64-byte limit.
    for row in telegram.job_buttons(9_999_999) + telegram.app_buttons(9_999_999):
        assert all(len(d.encode()) <= 64 for _, d in row)
