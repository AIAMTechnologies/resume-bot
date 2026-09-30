"""Dry runs never change what the bot does next, and the dashboard only serves files from the data dir."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from resumebot import db
from resumebot.config import DATA_DIR
from resumebot.engine import pipeline
from resumebot.models import Job, JobStatus, ReviewItem
from resumebot.sources.base import ApplyResult, Materials


@pytest.fixture
def no_tailoring(monkeypatch, tmp_path):
    async def fake_prepare(job):
        report = SimpleNamespace(score=90, keyword_coverage=0.8)
        return Materials(tmp_path / "r.pdf", tmp_path / "r.docx"), SimpleNamespace(report=report)
    monkeypatch.setattr(pipeline, "prepare", fake_prepare)


def _manual_cards(job_id: int) -> list:
    with db.session() as s:
        return list(s.exec(db.select(ReviewItem).where(ReviewItem.job_id == job_id)))


async def test_dry_run_of_linkedin_job_sends_nothing(no_tailoring):
    job = db.save(Job(source="linkedin", external_id="dry-1", company="DryCo", title="Analyst", url="u",
                      status=JobStatus.QUEUED, status_reason="score 90"))
    await pipeline.apply_job(job, dry_run=True)
    assert (job.status, job.status_reason) == (JobStatus.QUEUED, "score 90")
    assert _manual_cards(job.id) == []
    assert pipeline.company_recent_count("DryCo") == 0


async def test_dry_run_keeps_failed_job_failed(no_tailoring, monkeypatch):
    class Src:
        async def apply(self, ctx):
            return ApplyResult(submitted=False, note="dry run: stopped before submit")

    @asynccontextmanager
    async def fake_page(source=""):
        yield SimpleNamespace()

    monkeypatch.setitem(pipeline.SOURCES, "greenhouse", Src())
    monkeypatch.setattr(pipeline.browsers, "page", fake_page)
    monkeypatch.setattr(pipeline, "Human", lambda page: None)

    async def no_screenshot(*a, **k):
        return ""
    monkeypatch.setattr(pipeline, "_screenshot", no_screenshot)
    job = db.save(Job(source="greenhouse", external_id="dry-2", company="FailCo", title="Analyst", url="u",
                      status=JobStatus.FAILED, status_reason="timeout"))
    await pipeline.apply_job(job, dry_run=True)
    assert (job.status, job.status_reason) == (JobStatus.FAILED, "timeout")


async def test_file_endpoint_rejects_sibling_folders(tmp_path):
    from fastapi import HTTPException

    from resumebot.web.app import file
    sibling = DATA_DIR.resolve().parent / (DATA_DIR.resolve().name + "_backup")
    sibling.mkdir(exist_ok=True)
    secret = sibling / "secret.txt"
    secret.write_text("x")
    with pytest.raises(HTTPException):
        await file(str(secret))
    inside = DATA_DIR / "ok.txt"
    inside.write_text("ok")
    assert (await file(str(inside))).path == inside.resolve()
