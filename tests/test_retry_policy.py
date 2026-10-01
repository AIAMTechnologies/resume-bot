"""Failures are retried only when nothing could have been submitted; persistent ones stop."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from resumebot import db
from resumebot.engine import pipeline, questions
from resumebot.llm import base as llm_base
from resumebot.models import Application, Job, JobStatus, LearnedAnswer
from resumebot.sources.base import ApplyResult, Materials


@pytest.fixture
def fast_apply(monkeypatch, tmp_path):
    """No tailoring, no browser: the source adapter decides the outcome."""
    from resumebot.engine import drafts
    async def fake_prepare(job, refresh=False):
        return Materials(tmp_path / "r.pdf", tmp_path / "r.docx"), SimpleNamespace(report=SimpleNamespace(score=90, keyword_coverage=0.8))
    monkeypatch.setattr(drafts, "prepare", fake_prepare)

    @asynccontextmanager
    async def fake_page(source=""):
        yield SimpleNamespace()
    monkeypatch.setattr(pipeline.browsers, "page", fake_page)
    monkeypatch.setattr(pipeline, "Human", lambda page, brisk=False: None)

    async def no_screenshot(*a, **k):
        return ""
    monkeypatch.setattr(pipeline, "_screenshot", no_screenshot)
    monkeypatch.setattr(pipeline.telegram, "notify", lambda *a, **k: None)

    class Src:
        outcome = None
        async def apply(self, ctx):
            if callable(self.outcome):
                return self.outcome(ctx)
            raise self.outcome
    src = Src()
    monkeypatch.setitem(pipeline.SOURCES, "greenhouse", src)
    return src


def _job(**kw):
    return db.save(Job(source="greenhouse", external_id=uuid4().hex, company=kw.pop("company", "RetryCo"), title="Analyst",
                       url="u", status=JobStatus.QUEUED, status_reason="score 90", **kw))


async def test_pre_submit_problem_retries_once_then_fails(fast_apply):
    fast_apply.outcome = lambda ctx: ApplyResult(False, "submit button not found")
    job = _job()
    await pipeline.apply_job(job)
    assert job.status == JobStatus.QUEUED and job.status_reason.startswith("will retry (1/2)")
    assert pipeline.next_job("greenhouse") is None or pipeline.next_job("greenhouse").id != job.id  # waits before retrying
    await pipeline.apply_job(job)
    assert job.status == JobStatus.FAILED and job.apply_attempts == 2


async def test_unknown_state_after_submit_is_checked_not_retried(fast_apply):
    def clicked(ctx):
        ctx.submit_clicked = True
        return ApplyResult(False, "no confirmation; unknown state")
    fast_apply.outcome = clicked
    job = _job()
    app = await pipeline.apply_job(job)
    assert job.status == JobStatus.REVIEW and "possibly submitted" in job.status_reason
    assert app.status == "failed" and "possibly submitted" in app.error


async def test_validation_error_after_submit_fails_outright(fast_apply):
    def rejected(ctx):
        ctx.submit_clicked = True
        return ApplyResult(False, "no confirmation; Phone is required")
    fast_apply.outcome = rejected
    job = _job()
    await pipeline.apply_job(job)
    assert job.status == JobStatus.FAILED


async def test_transient_exception_before_submit_retries(fast_apply):
    fast_apply.outcome = TimeoutError("Page.goto: Timeout 30000ms exceeded")
    job = _job()
    await pipeline.apply_job(job)
    assert job.status == JobStatus.QUEUED and "will retry" in job.status_reason
    fast_apply.outcome = ValueError("selector broke")
    job2 = _job()
    await pipeline.apply_job(job2)
    assert job2.status == JobStatus.FAILED


async def test_tailoring_failure_requeues_once(monkeypatch):
    calls = []
    async def flaky(job):
        calls.append(1)
        raise RuntimeError("render exploded")
    monkeypatch.setattr(pipeline, "prepare", flaky)
    job = _job()
    await pipeline.apply_job(job)
    assert job.status == JobStatus.QUEUED and "will retry" in job.status_reason
    await pipeline.apply_job(job)
    assert job.status == JobStatus.FAILED and len(calls) == 2


async def test_scoring_failure_parks_job_after_three_tries(monkeypatch):
    async def broken(job):
        raise llm_base.LLMError("No JSON in model reply: hmm")
    monkeypatch.setattr(pipeline.matcher, "score", broken)
    monkeypatch.setattr(pipeline.matcher, "prefilter", lambda job: (True, ""))
    sent = []
    async def job_review(job, reason):
        sent.append(reason)
    monkeypatch.setattr(pipeline.review, "job_review", job_review)
    job = db.save(Job(source="ashby", external_id=uuid4().hex, company="C", title="Security Analyst", url="u",
                      description="A job"))
    for n in (1, 2):
        assert await pipeline.screen_job(job) == "error"
        assert job.status == JobStatus.NEW and job.score_attempts == n
    assert await pipeline.screen_job(job) == JobStatus.REVIEW
    assert sent and "scoring failed 3x" in sent[0]


async def test_company_routed_to_manual_after_repeated_failures(fast_apply, monkeypatch):
    from resumebot.engine import health
    cards = []
    async def manual_review(job, reason, *a):
        cards.append(reason)
    monkeypatch.setattr(pipeline.review, "manual_review", manual_review)
    health.route_company_manual("StuckCo", "form keeps failing")
    job = _job(company="StuckCo")
    await pipeline.apply_job(job)
    assert job.status == JobStatus.MANUAL and cards == ["form keeps failing"]
    assert health.company_manual("Other Co") == ""


async def test_complete_json_retries_once_on_bad_json(monkeypatch):
    import resumebot.llm as llm
    replies = iter(["Sure! Here you go:", '{"ok": true}'])
    class Flaky:
        async def complete(self, prompt, system="", max_tokens=0, context=""):
            return next(replies)
    monkeypatch.setattr(llm, "get_llm", lambda fast=False: Flaky())
    assert await llm.complete_json("x") == {"ok": True}


def test_ai_answers_to_closed_questions_are_remembered():
    log = {"Do you hold a G licence?": {"answer": "Yes", "origin": "llm (0.90)", "closed": True},
           "Why Acme?": {"answer": "Because…", "origin": "llm (0.95)", "closed": False},
           "Have you worked at Acme before?": {"answer": "No", "origin": "llm (0.9)", "closed": True},
           "First name": {"answer": "Ada", "origin": "config"}}
    assert questions.remember_ai_answers(log, "Acme") == 1
    assert questions.from_memory(questions.Field("Do you hold a G licence?", "radio", ["Yes", "No"])) == "Yes"
    assert questions.from_memory(questions.Field("Why Acme?")) is None
    with db.session() as s:
        row = s.exec(db.select(LearnedAnswer).where(LearnedAnswer.question == "Do you hold a G licence?")).one()
    assert row.origin == "ai"
    questions.remember("Do you hold a G licence?", "No")           # your answer overrides …
    questions.remember("Do you hold a G licence?", "Yes", origin="ai")  # … and the AI can't undo it
    assert questions.from_memory(questions.Field("Do you hold a G licence?")) == "No"
