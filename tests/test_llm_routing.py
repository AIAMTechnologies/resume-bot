"""Model routing (main vs fast) and prompt caching of the shared profile block."""
from types import SimpleNamespace

import pytest

import resumebot.llm as llm
from resumebot import config
from resumebot.engine import matcher, questions
from resumebot.engine.questions import Field
from resumebot.models import Job


@pytest.fixture
def api_env(monkeypatch):
    base = config.env()
    monkeypatch.setattr(config, "env", lambda: base.model_copy(update={
        "llm_backend": "anthropic_api", "llm_fallback": "", "anthropic_api_key": "sk-test",
        "llm_model": "claude-sonnet-5-5", "llm_fast_model": "claude-haiku-4-5"}))
    for mod in ("resumebot.llm", "resumebot.llm.anthropic_api"):
        monkeypatch.setattr(f"{mod}.env", config.env)
    llm._llm.cache_clear()
    yield
    llm._llm.cache_clear()


def test_fast_and_main_models(api_env):
    assert llm.get_llm().model == "claude-sonnet-5-5"
    assert llm.get_llm(fast=True).model == "claude-haiku-4-5"


def test_blank_fast_model_falls_back_to_main(api_env, monkeypatch):
    base = config.env()
    monkeypatch.setattr("resumebot.llm.env", lambda: base.model_copy(update={"llm_fast_model": ""}))
    assert llm.get_llm(fast=True).model == "claude-sonnet-5-5"


async def test_profile_context_is_cached_and_sent_first(api_env, monkeypatch):
    sent = {}

    async def create(**kw):
        sent.update(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text='{"ok": 1}')])

    api = llm.get_llm()
    monkeypatch.setattr(api, "client", SimpleNamespace(messages=SimpleNamespace(create=create)))
    assert await llm.complete_json("the question", system="sys", context="PROFILE") == {"ok": 1}
    first, second = sent["messages"][0]["content"]
    assert first == {"type": "text", "text": "PROFILE", "cache_control": {"type": "ephemeral"}}
    assert second["text"].startswith("the question") and "cache_control" not in second


async def test_callers_pick_the_right_model(monkeypatch):
    seen = []

    async def fake(prompt, system="", max_tokens=4000, *, context="", fast=False):
        seen.append((system[:20], fast, context.startswith("CANDIDATE PROFILE")))
        return {"score": 80, "answer": "x", "grounded": True, "confidence": 0.9}

    monkeypatch.setattr(matcher, "complete_json", fake)
    monkeypatch.setattr(questions, "complete_json", fake)
    await matcher.score(Job(source="x", external_id="1", company="c", title="t", url="u"))
    await questions.from_llm(Field("Years of Splunk experience?", "text"), "t", "c", "d")
    await questions.from_llm(Field("Why do you want to work here?", "textarea"), "t", "c", "d")
    # Two-stage scoring: fast screen, then the main model for promising jobs (80 here); short answers use fast.
    assert [(fast, ctx) for _, fast, ctx in seen] == [(True, True), (False, True), (True, True), (False, True)]
    from types import SimpleNamespace
    monkeypatch.setattr(matcher, "env", lambda: SimpleNamespace(score_with_fast_model=True))
    seen.clear()
    await matcher.score(Job(source="x", external_id="1", company="c", title="t", url="u"))
    assert seen[0][1] is True  # SCORE_WITH_FAST_MODEL=true opts back into the cheaper model



async def test_scorer_is_told_work_authorization(monkeypatch):
    seen = []
    async def fake(prompt, system="", max_tokens=4000, *, context="", fast=False):
        seen.append((prompt, system))
        return {"score": 80}
    monkeypatch.setattr(matcher, "complete_json", fake)
    await matcher.score(Job(source="x", external_id="1", company="c", title="t", url="u", location="San Francisco"))
    prompt, system = seen[0]
    assert "CANDIDATE LOGISTICS" in prompt and "Authorized to work in the US today" in prompt
    assert "export-control" in system and "relocate" in system



async def test_low_fast_score_skips_the_expensive_second_pass(monkeypatch):
    calls = []
    async def fake(prompt, system="", max_tokens=4000, *, context="", fast=False):
        calls.append(fast)
        return {"score": 30, "reasons": ["poor fit"]}
    monkeypatch.setattr(matcher, "complete_json", fake)
    score, _, _ = await matcher.score(Job(source="x", external_id="1", company="c", title="t", url="u"))
    assert score == 30 and calls == [True]  # one cheap call, no main-model call
