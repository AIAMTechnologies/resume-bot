"""One AI call per form, several jobs screened at once, consistent HTTP headers."""
import asyncio
from pathlib import Path

import pytest

from resumebot.engine import pipeline, questions
from resumebot.engine.questions import Answerer, Field
from resumebot.sources import http


async def test_prefetch_batches_unknown_questions_by_model(monkeypatch):
    calls = []

    async def batch(fields, title, company, desc, fast):
        calls.append((sorted(f.label for f in fields), fast))
        return {f.label: ("Yes" if f.options else "Because of the SOC work.", True, 0.95) for f in fields}

    async def single(*a, **k):
        raise AssertionError("per-question AI call must not happen after a prefetch")
    monkeypatch.setattr(questions, "from_llm_batch", batch)
    monkeypatch.setattr(questions, "from_llm", single)
    ans = Answerer("Analyst", "Acme", "desc")
    fields = [Field("First Name", "text"), Field("Do you hold a G licence?", "radio", ["Yes", "No"], True),
              Field("Are you CISSP certified?", "select", ["Yes", "No"]), Field("Why Acme?", "textarea", None, True)]
    assert await ans.prefetch(fields) == 3  # First Name comes from answers.yaml
    assert calls == [(["Are you CISSP certified?", "Do you hold a G licence?"], True), (["Why Acme?"], False)]
    assert await ans(Field("Do you hold a G licence?", "radio", ["Yes", "No"], True)) == "Yes"
    assert "SOC" in await ans(Field("Why Acme?", "textarea", None, True))
    assert ans.log["Do you hold a G licence?"]["origin"].startswith("llm")
    assert await ans(Field("First Name", "text")) == "Ada"


async def test_batch_failure_falls_back_to_single_calls(monkeypatch):
    async def batch(*a, **k):
        raise RuntimeError("model hiccup")
    singles = []

    async def single(f, *a):
        singles.append(f.label)
        return "No", True, 0.9
    monkeypatch.setattr(questions, "from_llm_batch", batch)
    monkeypatch.setattr(questions, "from_llm", single)
    ans = Answerer("Analyst", "Acme", "desc")
    await ans.prefetch([Field("Clearance?", "radio", ["Yes", "No"], True)])
    assert await ans(Field("Clearance?", "radio", ["Yes", "No"], True)) == "No" and singles == ["Clearance?"]


async def test_form_filler_prefetches_once_per_scan(tmp_path, monkeypatch):
    pw_api = pytest.importorskip("playwright.async_api")
    from resumebot.sources import forms
    from resumebot.sources.base import ApplyContext, Materials
    from tests.test_form_filler import FastHuman, _launch
    page_html = """<form>
      <label for="a">Do you hold a G licence?*</label><select id="a" required><option value="">Select</option><option>Yes</option><option>No</option></select>
      <label for="b">Years with Splunk*</label><input id="b" required>
      <label for="c">Why us?*</label><textarea id="c" required></textarea>
      <fieldset><legend>Can you work shifts?*</legend><label><input type="radio" name="s" value="y">Yes</label><label><input type="radio" name="s" value="n">No</label></fieldset>
    </form>"""
    seen = []

    class Batcher:
        async def prefetch(self, fields):
            seen.append(sorted(f.label for f in fields))
            return len(fields)

        async def __call__(self, f):
            return {"Do you hold a G licence?*": "Yes", "Years with Splunk*": "3", "Why us?*": "Because.",
                    "Can you work shifts?*": "Yes"}.get(f.label, "")

    async with pw_api.async_playwright() as pw:
        try:
            browser = await _launch(pw)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Chromium unavailable: {e}")
        page = await browser.new_page()
        await page.set_content(page_html)
        ctx = ApplyContext(job=None, page=page, human=FastHuman(page), materials=Materials(Path("x"), Path("y")),
                           answer=Batcher())
        filled = await forms.fill_form(ctx, "form")
        await browser.close()
    assert seen[0] == ["Can you work shifts?*", "Do you hold a G licence?*", "Why us?*", "Years with Splunk*"]
    assert filled["Years with Splunk*"] == "3" and filled["Can you work shifts?*"] == "Yes"


async def test_triage_screens_jobs_concurrently(monkeypatch):
    from uuid import uuid4
    from resumebot import db
    from resumebot.models import Job
    running, peak = 0, 0

    async def slow_screen(job, progress=""):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        job.status, job.match_score = "skipped", 10
        db.save(job)
        return "skipped"
    monkeypatch.setattr(pipeline, "screen_job", slow_screen)
    for _ in range(6):
        db.save(Job(source="ashby", external_id=uuid4().hex, company="C", title="Security Analyst", url="u"))
    result = await pipeline.triage_new(limit=6)
    assert result.startswith("Screened 6 job(s), 6 scored") and 2 <= peak <= 3


def test_headers_look_like_the_same_chrome():
    html, api = http.headers("html", referer="https://www.linkedin.com/jobs/"), http.headers("json")
    assert html["User-Agent"] == api["User-Agent"] and "Chrome/" in html["User-Agent"]
    assert http.chrome_major() in html["sec-ch-ua"] and html["sec-fetch-dest"] == "document"
    assert api["Accept"].startswith("application/json") and api["sec-fetch-mode"] == "cors"
    assert html["Referer"] == "https://www.linkedin.com/jobs/"
