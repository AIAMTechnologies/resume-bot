"""End-to-end application scenarios.

Runs the real pipeline (discovery → triage → tailoring → PDF render → browser form filling →
submit → bookkeeping) in a real Chromium against local copies of Greenhouse / Lever / Ashby
style application pages. Only the network edges are faked: the LLM, Telegram, and the job-board
HTTP API. Pages are served by intercepting the real ATS URLs inside the browser, so the bot's
own URL handling is exercised too.
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from resumebot import config, db
from resumebot.browser import human as human_mod
from resumebot.browser import session as session_mod
from resumebot.engine import pipeline, questions, review
from resumebot.engine.pacing import Decision
from resumebot.models import Application, AppStatus, Job, JobStatus, ProfileItem, ReviewItem
from resumebot.notify import telegram
from resumebot.sources import ats_boards, forms

pytest.importorskip("playwright.async_api")

# Work in progress: slow (real browser) and some scenarios document known gaps, so opt-in only.
pytestmark = pytest.mark.skipif(os.environ.get("RESUMEBOT_E2E") != "1",
                                reason="set RESUMEBOT_E2E=1 to run browser scenarios")

# ---------------------------------------------------------------- persona & profile

ANSWERS = {
    "contact": {"first_name": "Sam", "last_name": "Rivera", "email": "sam.rivera@example.com",
                "phone": "416-555-0142", "city": "Toronto", "province_state": "ON", "country": "Canada",
                "postal_code": "M5V 2T6", "linkedin": "https://linkedin.com/in/samrivera",
                "github": "https://github.com/samrivera", "portfolio": ""},
    "work_authorization": {"canada": "Yes", "united_states": "No", "requires_sponsorship_canada": "No",
                           "requires_sponsorship_us": "Yes", "willing_to_relocate": "Yes"},
    "logistics": {"notice_period": "2 weeks", "earliest_start": "2 weeks from offer",
                  "desired_salary_cad": "95000", "desired_salary_usd": "75000", "open_to_remote": "Yes",
                  "open_to_hybrid": "Yes", "open_to_onsite": "Yes", "years_experience_total": "4",
                  "highest_education": "Bachelor's degree"},
    "application_consent": True,
    "eeo": {"gender": "Decline to answer", "race_ethnicity": "Decline to answer",
            "veteran_status": "Decline to answer", "disability": "Decline to answer"},
    "custom": {"How did you hear about us?": "Job board"},
}

PROFILE = [
    dict(kind="experience", title="Security Analyst", organization="Northwind Bank", location="Toronto, ON",
         start="2022-03", end="Present",
         bullets=["Triaged 40+ SIEM alerts per shift in Splunk and escalated confirmed incidents",
                  "Wrote Python scripts that cut phishing triage time by 30%",
                  "Led incident response for 12 malware cases end to end"],
         skills=["Splunk", "SIEM", "Python", "Incident Response"]),
    dict(kind="experience", title="IT Support Specialist", organization="Maple Health", location="Toronto, ON",
         start="2020-01", end="2022-02",
         bullets=["Managed Active Directory accounts for 800 staff", "Rolled out MFA to all remote users"],
         skills=["Active Directory", "MFA"]),
    dict(kind="project", title="Home SOC Lab", organization="Personal", start="2023", end="2024",
         bullets=["Built a detection lab with Wazuh and Sysmon on AWS", "Wrote 15 Sigma detection rules"],
         skills=["AWS", "Wazuh", "Sigma", "Threat Detection"]),
    dict(kind="education", title="BSc Computer Science", organization="York University", start="2016", end="2020"),
    dict(kind="certification", title="CompTIA Security+", organization="CompTIA", start="2021", end=""),
]

JD = ("Acme is hiring a Security Analyst in Toronto. You will monitor our SIEM (Splunk), lead incident "
      "response, write Python automation, and build threat detection content on AWS. " * 3)


# ---------------------------------------------------------------- fakes: LLM & Telegram

class FakeLLM:
    """Answers like a careful model: grounded facts only, conservative otherwise."""

    def __init__(self):
        self.calls: list[str] = []

    async def complete(self, prompt: str, system: str = "", max_tokens: int = 1000, context: str = "") -> str:
        from resumebot.profile import master
        if "technical recruiter" in system:
            self.calls.append("score")
            return json.dumps({"score": 88, "reasons": ["Splunk + IR experience"], "missing": [], "hard_blocker": ""})
        if "expert resume writer" in system:
            self.calls.append("tailor")
            items = master.all_items()
            sec = lambda k: [i for i in items if i.kind == k]  # noqa: E731
            entry = lambda i: {"source_ids": [i.id], "title": i.title, "organization": i.organization,  # noqa: E731
                               "bullets": [{"text": b, "source_ids": [i.id]} for b in i.bullets]}
            return json.dumps({
                "jd_keywords": ["Splunk", "SIEM", "incident response", "Python", "AWS", "threat detection"],
                "headline": "Security Analyst — SIEM, Incident Response, Python",
                "summary": "Security analyst with hands-on Splunk, incident response and Python automation.",
                "skills": [{"category": "Security", "items": ["Splunk", "SIEM", "Incident Response",
                                                              "Threat Detection"]},
                           {"category": "Tools", "items": ["Python", "AWS", "Wazuh"]}],
                "experience": [entry(i) for i in sec("experience")],
                "projects": [{**entry(i), "stack": i.skills} for i in sec("project")],
                "education": [{**entry(i), "bullets": []} for i in sec("education")],
                "certifications": [{"source_ids": [i.id], "title": i.title} for i in sec("certification")],
                "unsupported_keywords": [],
            })
        if "job application questions" in system:
            q = prompt.split("QUESTION:", 1)[1].split("\n", 1)[0].strip().lower()
            self.calls.append(f"answer:{q}")
            if re.search(r"why|interest", q):
                return json.dumps({"answer": "Your SOC work matches my Splunk and incident response experience.",
                                   "grounded": True, "confidence": 0.9})
            if re.search(r"location|city", q):
                return json.dumps({"answer": "Toronto", "grounded": True, "confidence": 0.95})
            if "current company" in q:
                return json.dumps({"answer": "Northwind Bank", "grounded": True, "confidence": 0.95})
            return json.dumps({"answer": "", "grounded": False, "confidence": 0.2})
        if "cover letter" in system.lower():
            self.calls.append("cover")
            return "I have run incident response at Northwind Bank and built detections on AWS. " * 4
        self.calls.append("other")
        return "Hi — I just applied for the Security Analyst role and would love to connect."


@pytest.fixture
def world(monkeypatch, tmp_path):
    """Persona, profile, fake LLM/Telegram, fast human timing, and a real headless Chromium."""
    import resumebot.llm as llm_mod

    for mod in (config, pipeline, forms, questions):
        monkeypatch.setattr(mod, "answers", lambda: ANSWERS)
    llm = FakeLLM()
    monkeypatch.setattr(llm_mod, "get_llm", lambda fast=False: llm)

    with db.session() as s:  # fresh profile for every scenario
        for it in s.exec(db.select(ProfileItem)):
            s.delete(it)
        s.commit()
    for p in PROFILE:
        db.save(ProfileItem(**{"bullets": [], "skills": [], **p}))
    db.kv_set("target_titles", ["Security Analyst"])

    sent: list[str] = []

    async def send(text, buttons=None, reply_to=None):
        sent.append(text)
        return len(sent)

    async def send_document(path, caption=""):
        sent.append(f"[document] {Path(path).name}")
        return len(sent)

    monkeypatch.setattr(telegram, "send", send)
    monkeypatch.setattr(telegram, "send_document", send_document)
    monkeypatch.setattr(telegram, "notify", lambda text, buttons=None: sent.append(text))

    # Human timing at 1% so reading/typing takes seconds, not minutes. Same code paths.
    real_sleep = asyncio.sleep
    monkeypatch.setattr(human_mod, "asyncio", SimpleNamespace(
        sleep=lambda s: real_sleep(s * 0.01), get_event_loop=asyncio.get_event_loop))

    sites = Sites()

    async def launch(self):
        if self._pw is None:
            self._pw = await session_mod.async_playwright().start()
        exe = os.environ.get("RESUMEBOT_TEST_CHROMIUM") or (
            "/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None)
        try:
            browser = await self._pw.chromium.launch(headless=True, executable_path=exe)
        except Exception:  # noqa: BLE001 — no bundled Chromium; use installed Google Chrome
            try:
                browser = await self._pw.chromium.launch(headless=True, channel="chrome")
            except Exception as e:  # noqa: BLE001
                pytest.skip(f"Chromium unavailable: {e}")
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-CA")
        ctx.on("close", lambda _: self._forget())
        await ctx.route(session_mod.BLOCKED_HOSTS, lambda route: route.abort("blockedbyclient"))
        await ctx.route("**/*", sites.handle)
        return ctx

    monkeypatch.setattr(session_mod.BrowserManager, "_launch", launch)
    monkeypatch.setattr(pipeline.Gate, "check", lambda self: Decision(True))
    yield SimpleNamespace(llm=llm, telegram=sent, sites=sites)


@pytest.fixture(autouse=True)
async def _close_browser():
    yield
    await pipeline.browsers.shutdown()


# ---------------------------------------------------------------- fake ATS websites

class Sites:
    """Serves pages for real ATS URLs inside the browser and records what gets submitted."""

    def __init__(self):
        self.pages: dict[str, str] = {}
        self.submissions: list[dict] = []
        self.submit_reply: dict = {"ok": True}

    async def handle(self, route, request):
        url = request.url
        if request.method == "POST" and url.endswith("/__submit"):
            self.submissions.append(json.loads(request.post_data or "{}"))
            return await route.fulfill(status=200, content_type="application/json", body=json.dumps(self.submit_reply))
        for prefix in sorted(self.pages, key=len, reverse=True):
            if url.startswith(prefix):
                return await route.fulfill(status=200, content_type="text/html", body=self.pages[prefix])
        return await route.abort()


FORM_JS = r"""
<script>
function combos(root) {
  root.querySelectorAll('[data-combo]').forEach(w => {
    if (w.__ready) return; w.__ready = true;
    const input = w.querySelector('input'), shown = w.querySelector('.select__single-value');
    const opts = JSON.parse(w.dataset.combo); let menu = null;
    const close = () => { if (menu) { menu.remove(); menu = null; } input.setAttribute('aria-expanded', 'false'); };
    const pick = o => { w.dataset.value = o; shown.textContent = o; input.value = ''; close();
                        w.dispatchEvent(new CustomEvent('picked', {bubbles: true, detail: o})); };
    const open = () => { close(); menu = document.createElement('div'); menu.className = 'select__menu';
      menu.setAttribute('role', 'listbox'); const q = input.value.toLowerCase();
      opts.filter(o => o.toLowerCase().includes(q)).forEach(o => { const d = document.createElement('div');
        d.className = 'select__option'; d.setAttribute('role', 'option'); d.textContent = o;
        d.addEventListener('mousedown', e => { e.preventDefault(); pick(o); }); menu.appendChild(d); });
      w.appendChild(menu); input.setAttribute('aria-expanded', 'true'); };
    input.addEventListener('focus', open); input.addEventListener('click', open); input.addEventListener('input', open);
    input.addEventListener('keydown', e => { if (e.key === 'Escape') close();
      if (e.key === 'Enter') { e.preventDefault(); const f = menu && menu.querySelector('[role=option]'); if (f) pick(f.textContent); } });
    input.addEventListener('blur', () => setTimeout(close, 150));
  });
  root.querySelectorAll('[data-yesno]').forEach(g => g.querySelectorAll('button').forEach(b =>
    b.addEventListener('click', () => { g.dataset.value = b.textContent.trim(); b.setAttribute('aria-pressed', 'true'); })));
}
function collect(form) {
  const out = {};
  form.querySelectorAll('input, textarea, select').forEach(el => {
    const k = el.name || el.id; if (!k) return;
    const combo = el.closest('[data-combo]'); if (combo) { out[k] = combo.dataset.value || ''; return; }
    if (el.type === 'file') out[k] = [...el.files].map(f => f.name).join(',');
    else if (el.type === 'radio') { if (el.checked) out[k] = el.value; else if (!(k in out)) out[k] = ''; }
    else if (el.type === 'checkbox') out[k] = el.checked;
    else out[k] = el.value;
  });
  form.querySelectorAll('[data-yesno]').forEach(g => out[g.dataset.yesno] = g.dataset.value || '');
  return out;
}
document.addEventListener('DOMContentLoaded', () => {
  const form = document.querySelector('form[data-required]'); combos(document);
  form.addEventListener('submit', async e => {
    e.preventDefault(); document.querySelectorAll('.error').forEach(x => x.remove());
    const data = collect(form);
    const miss = JSON.parse(form.dataset.required).filter(k => document.getElementById('wrap-' + k)?.hidden ? false : !data[k]);
    if (miss.length) { miss.forEach(k => { const d = document.createElement('div'); d.className = 'error';
      d.setAttribute('role', 'alert'); d.textContent = k + ' is required'; form.appendChild(d); }); return; }
    if (form.dataset.captcha) { const f = document.createElement('iframe');
      f.src = 'https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html#frame=challenge';
      f.style.cssText = 'width:400px;height:500px'; document.body.appendChild(f); return; }
    const r = await fetch('/__submit', {method: 'POST', body: JSON.stringify(data)}); const j = await r.json();
    if (j.ok) document.body.innerHTML = '<h1>' + form.dataset.success + '</h1>';
    else { const d = document.createElement('div'); d.className = 'error'; d.setAttribute('role', 'alert');
      d.textContent = j.error; form.appendChild(d); }
  });
});
</script>
<style>.visually-hidden{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
.select__control{border:1px solid #999;min-height:32px;position:relative}.select__input{width:120px}
.select__menu{border:1px solid #ccc;background:#fff}.select__option{padding:4px}.field{margin:10px 0}</style>
"""


def combo(key: str, label: str, options: list[str], required: bool = True) -> str:
    star = "*" if required else ""
    return (f'<div class="field" id="wrap-{key}"><label id="{key}-label" for="{key}">{label}{star}</label>'
            f'<div class="select__control" data-combo="{html.escape(json.dumps(options))}">'
            f'<div class="select__single-value"></div>'
            f'<input id="{key}" class="select__input" role="combobox" aria-autocomplete="list" '
            f'aria-expanded="false" {"aria-required=true" if required else ""}></div></div>')


def text(key: str, label: str, required: bool = True, kind: str = "text") -> str:
    star = "*" if required else ""
    tag = (f'<textarea id="{key}" name="{key}"></textarea>' if kind == "textarea"
           else f'<input id="{key}" name="{key}" type="{kind}" {"aria-required=true" if required else ""}>')
    return f'<div class="field" id="wrap-{key}"><label for="{key}">{label}{star}</label>{tag}</div>'


def greenhouse_page(extra: str = "", required_extra: list[str] = (), success: str = "Thank you for applying.",
                    captcha: bool = False) -> str:
    required = ["first_name", "last_name", "email", "phone", "candidate-location", "resume",
                "question_auth", "question_sponsor", "question_why", "consent", *required_extra]
    return f"""<!doctype html><html><head><title>Job Application for Security Analyst at Acme</title>{FORM_JS}</head>
<body><h1>Security Analyst</h1><div class="job__description"><p>{JD}</p></div>
<form id="application-form" novalidate data-required='{json.dumps(required)}' data-success="{success}"
      {'data-captcha="1"' if captcha else ''}>
  <h2>Apply for this job</h2>
  {text("first_name", "First Name")}{text("last_name", "Last Name")}
  {text("email", "Email")}{text("phone", "Phone", kind="tel")}
  {combo("candidate-location", "Location (City)", ["Toronto, Ontario, Canada", "Toronto, Ohio, United States",
                                                   "Vancouver, British Columbia, Canada"])}
  <div class="field file-upload" id="wrap-resume"><div id="upload-label-resume">Resume/CV*</div>
    <label for="resume">Resume/CV</label><button type="button">Attach</button>
    <input id="resume" type="file" class="visually-hidden" aria-required="true"></div>
  <div class="field file-upload" id="wrap-cover_letter"><div id="upload-label-cover">Cover Letter</div>
    <label for="cover_letter">Cover Letter</label><button type="button">Attach</button>
    <input id="cover_letter" type="file" class="visually-hidden"></div>
  {text("question_linkedin", "LinkedIn Profile", required=False)}
  {combo("question_auth", "Are you legally authorized to work in Canada?", ["Yes", "No"])}
  {combo("question_sponsor", "Will you now or in the future require sponsorship for employment visa status?", ["Yes", "No"])}
  {text("question_why", "Why do you want to work at Acme?", kind="textarea")}
  {text("question_hear", "How did you hear about us?", required=False)}
  {extra}
  <h3>Voluntary Self-Identification</h3>
  {combo("gender", "Gender", ["Male", "Female", "Decline To Self Identify"], required=False)}
  {combo("veteran_status", "Veteran Status", ["I am a veteran", "I am not a veteran", "I don't wish to answer"], required=False)}
  <div class="field" id="wrap-consent"><label><input type="checkbox" id="consent" name="consent" aria-required="true">
    I agree to the Acme privacy policy and the processing of my data for recruiting purposes.*</label></div>
  <button type="submit">Submit application</button>
</form></body></html>"""


GH_URL = "https://job-boards.greenhouse.io/embed/job_app?for={slug}&token={token}"


def greenhouse_api(slug: str, token: int, company: str) -> httpx.MockTransport:
    now = datetime.now(timezone.utc).isoformat()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "boards-api.greenhouse.io" and slug in request.url.path
        return httpx.Response(200, json={"jobs": [{
            "id": token, "title": "Security Analyst", "company_name": company, "updated_at": now,
            "location": {"name": "Toronto, Ontario, Canada"},
            "absolute_url": f"https://job-boards.greenhouse.io/{slug}/jobs/{token}",
            "content": "&lt;p&gt;" + JD + "&lt;/p&gt;"}]})
    return httpx.MockTransport(handler)


def make_job(source: str, company: str, url: str, apply_url: str = "", ext: str = "") -> Job:
    return db.save(Job(source=source, external_id=ext or f"{company}:{url}", company=company, title="Security Analyst",
                       url=url, apply_url=apply_url, location="Toronto, ON", description=JD, match_score=88,
                       status=JobStatus.QUEUED, status_reason="score 88"))


def app_for(job: Job) -> Application | None:
    with db.session() as s:
        return s.exec(db.select(Application).where(Application.job_id == job.id)).first()


def refreshed(job: Job) -> Job:
    with db.session() as s:
        return s.get(Job, job.id)


# ================================================================ scenarios

async def test_s1_greenhouse_discover_to_submitted(world, monkeypatch):
    """Board API → triage → tailored PDF → form filled in the browser → submitted → recorded."""
    slug, token, company = "acme-s1", 5001, "Acme S1"
    world.sites.pages[GH_URL.format(slug=slug, token=token)] = greenhouse_page()
    transport = greenhouse_api(slug, token, company)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(ats_boards.httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw))
    monkeypatch.setattr(ats_boards, "companies", lambda: {"greenhouse": [slug]})

    assert await pipeline.discover("greenhouse") == 1
    await pipeline.triage_new()
    with db.session() as s:
        job = s.exec(db.select(Job).where(Job.external_id == f"{slug}:{token}")).one()
    assert job.status == JobStatus.QUEUED, job.status_reason

    assert (await pipeline.tick_source("greenhouse")).startswith(f"#{job.id}: applied")
    job = refreshed(job)
    app = app_for(job)
    assert job.status == JobStatus.APPLIED, job.status_reason
    assert app and app.status == AppStatus.SUBMITTED

    [sub] = world.sites.submissions
    assert sub["first_name"] == "Sam" and sub["last_name"] == "Rivera"
    assert sub["email"] == "sam.rivera@example.com" and sub["phone"] == "416-555-0142"
    assert sub["candidate-location"].startswith("Toronto, Ontario")
    assert sub["resume"].endswith("Resume.pdf") and sub["cover_letter"] == "Cover_Letter.pdf"
    assert sub["question_auth"] == "Yes" and sub["question_sponsor"] == "No"
    assert "Splunk" in sub["question_why"]
    assert sub["question_linkedin"] == "https://linkedin.com/in/samrivera"
    assert sub["question_hear"] == "Job board"
    assert sub["gender"] == "Decline To Self Identify"
    assert sub["veteran_status"] == "I don't wish to answer"
    assert sub["consent"] is True
    assert any("Applied" in m for m in world.telegram)


async def test_s2_unknown_question_goes_to_review_then_submits(world, monkeypatch):
    """Clearance question + consent box (consent off) → review cards → you answer → next run submits."""
    monkeypatch.setitem(ANSWERS, "application_consent", False)
    extra = combo("question_clearance", "Do you currently hold an active Secret security clearance?", ["Yes", "No"])
    job = make_job("greenhouse", "Acme S2", "https://job-boards.greenhouse.io/acme-s2/jobs/1",
                   GH_URL.format(slug="acme-s2", token=2), ext="acme-s2:2")
    world.sites.pages[job.apply_url] = greenhouse_page(extra, ["question_clearance"])

    await pipeline.apply_job(job)
    assert refreshed(job).status == JobStatus.REVIEW
    assert world.sites.submissions == []
    with db.session() as s:
        cards = list(s.exec(db.select(ReviewItem).where(ReviewItem.job_id == job.id, ReviewItem.kind == "question")))
    asked = sorted(c.question for c in cards)
    assert len(asked) == 2 and "clearance" in asked[0].lower() and "privacy" in asked[1].lower(), asked

    for c in cards:  # you reply in Telegram
        review.resolve(c.id, "answer", "No" if "clearance" in c.question.lower() else "check")
    job = refreshed(job)
    assert job.status == JobStatus.QUEUED
    await pipeline.apply_job(job)
    assert refreshed(job).status == JobStatus.APPLIED, refreshed(job).status_reason
    sub = world.sites.submissions[-1]
    assert sub["question_clearance"] == "No" and sub["consent"] is True


LEVER_POSTING = """<!doctype html><html><body><div class="posting-headline"><h2>Security Analyst</h2></div>
<div class="section">{jd}</div><a class="postings-btn" href="{apply}">Apply for this job</a></body></html>"""


def lever_form(captcha: bool = False) -> str:
    required = ["resume", "name", "email", "phone", "cards[auth]", "cards[why]", "consent"]
    return f"""<!doctype html><html><head>{FORM_JS}</head><body><h2>Security Analyst</h2>
<form id="application-form" novalidate data-required='{json.dumps(required)}' data-success="Application submitted!"
      {'data-captcha="1"' if captcha else ''}>
  <div class="field" id="wrap-resume"><label for="resume-upload-input">Attach resume/CV ✱</label>
    <input type="file" id="resume-upload-input" name="resume"></div>
  {text("name", "Full name ✱")}{text("email", "Email ✱")}{text("phone", "Phone ✱", kind="tel")}
  {text("org", "Current company", required=False)}
  {text("urls[LinkedIn]", "LinkedIn URL", required=False)}{text("urls[GitHub]", "GitHub URL", required=False)}
  <fieldset id="wrap-cards[auth]"><legend>Are you legally authorized to work in Canada? ✱</legend>
    <label><input type="radio" name="cards[auth]" value="Yes">Yes</label>
    <label><input type="radio" name="cards[auth]" value="No">No</label></fieldset>
  {text("cards[why]", "Why are you interested in Acme? ✱", kind="textarea")}
  <div class="field"><label for="eeo-gender">Gender</label><select id="eeo-gender" name="eeo[gender]">
    <option value="Select ...">Select ...</option><option value="Male">Male</option><option value="Female">Female</option>
    <option value="Decline to self-identify">Decline to self-identify</option></select></div>
  <div class="field"><label for="eeo-race">Race</label><select id="eeo-race" name="eeo[race]">
    <option value="">Select ...</option><option value="White">White</option><option value="Asian">Asian</option>
    <option value="Decline to self-identify">Decline to self-identify</option></select></div>
  <div class="field" id="wrap-consent"><label><input type="checkbox" id="consent" name="consent">
    I agree to Acme's privacy policy and consent to the processing of my data ✱</label></div>
  <button type="submit">Submit application</button>
</form></body></html>"""


async def test_s3_lever_posting_to_submitted(world):
    job = make_job("lever", "Acme S3", "https://jobs.lever.co/acme-s3/0f1e2d3c",
                   "https://jobs.lever.co/acme-s3/0f1e2d3c/apply", ext="acme-s3:0f1e2d3c")
    world.sites.pages[job.url + "/apply"] = lever_form()
    world.sites.pages[job.url] = LEVER_POSTING.format(jd=JD, apply=job.apply_url)

    await pipeline.apply_job(job)
    job = refreshed(job)
    assert job.status == JobStatus.APPLIED, job.status_reason
    [sub] = world.sites.submissions
    assert sub["name"] == "Sam Rivera" and sub["email"] == "sam.rivera@example.com"
    assert sub["resume"].endswith("Resume.pdf")
    assert sub["cards[auth]"] == "Yes" and "Splunk" in sub["cards[why]"]
    assert sub["urls[LinkedIn]"] == "https://linkedin.com/in/samrivera"
    assert sub["eeo[gender]"] == "Decline to self-identify" and sub["eeo[race]"] == "Decline to self-identify"
    assert sub["consent"] is True


def ashby_form() -> str:
    required = ["_systemfield_name", "_systemfield_email", "_systemfield_resume", "q_auth", "q_sponsor"]
    return f"""<!doctype html><html><head>{FORM_JS}</head><body>
<div class="ashby-job-posting-heading">Security Analyst</div>
<form class="ashby-application-form-container" novalidate data-required='{json.dumps(required)}'
      data-success="Thanks for applying! Your application was successfully submitted.">
  {text("_systemfield_name", "Name")}{text("_systemfield_email", "Email", kind="email")}
  <div class="field" id="wrap-_systemfield_resume"><label for="_systemfield_resume">Resume*</label>
    <input type="file" id="_systemfield_resume" name="_systemfield_resume"></div>
  <div class="field" id="wrap-q_auth" data-yesno="q_auth"><label>Are you legally authorized to work in Canada?*</label>
    <div><button type="button">Yes</button><button type="button">No</button></div></div>
  <div class="field" id="wrap-q_sponsor" data-yesno="q_sponsor"><label>Will you require visa sponsorship?*</label>
    <div><button type="button">Yes</button><button type="button">No</button></div></div>
  <button type="submit">Submit Application</button>
</form></body></html>"""


async def test_s4_ashby_yes_no_button_questions(world):
    """Ashby renders yes/no questions as two buttons, not radios or a select."""
    job = make_job("ashby", "Acme S4", "https://jobs.ashbyhq.com/acme-s4/7a9b",
                   "https://jobs.ashbyhq.com/acme-s4/7a9b/application", ext="acme-s4:7a9b")
    world.sites.pages[job.url] = ashby_form()
    await pipeline.apply_job(job)
    job = refreshed(job)
    report(job, world)
    assert job.status == JobStatus.APPLIED, job.status_reason
    assert world.sites.submissions[-1]["q_auth"] == "Yes"


async def test_s5_follow_up_question_revealed_by_an_answer(world):
    """Answering 'willing to relocate: Yes' reveals a required follow-up the first scan never saw."""
    extra = (combo("question_relocate", "Are you willing to relocate?", ["Yes", "No"])
             + '<div class="field" id="wrap-question_where" hidden><label for="question_where">'
               'Which cities would you relocate to?*</label><input id="question_where" name="question_where" '
               'aria-required="true"></div>'
             + """<script>document.addEventListener('picked', e => { if (e.target.querySelector('#question_relocate'))
                  document.getElementById('wrap-question_where').hidden = e.detail !== 'Yes'; });</script>""")
    job = make_job("greenhouse", "Acme S5", "https://job-boards.greenhouse.io/acme-s5/jobs/5",
                   GH_URL.format(slug="acme-s5", token=5), ext="acme-s5:5")
    world.sites.pages[job.apply_url] = greenhouse_page(extra, ["question_relocate", "question_where"])
    await pipeline.apply_job(job)
    job = refreshed(job)
    report(job, world)
    # Wanted: the follow-up goes to review (REVIEW) instead of a failed submit.
    assert job.status == JobStatus.REVIEW, job.status_reason


async def test_s6_site_rejects_submission(world):
    job = make_job("greenhouse", "Acme S6", "https://job-boards.greenhouse.io/acme-s6/jobs/6",
                   GH_URL.format(slug="acme-s6", token=6), ext="acme-s6:6")
    world.sites.pages[job.apply_url] = greenhouse_page()
    world.sites.submit_reply = {"ok": False, "error": "There was a problem with your application. Please try again."}
    app = await pipeline.apply_job(job)
    job = refreshed(job)
    assert job.status == JobStatus.FAILED and "problem with your application" in job.status_reason
    assert app.status == AppStatus.FAILED and "problem" in app.error


async def test_s7_captcha_after_submit_pauses_source(world):
    job = make_job("lever", "Acme S7", "https://jobs.lever.co/acme-s7/1a2b",
                   "https://jobs.lever.co/acme-s7/1a2b/apply", ext="acme-s7:1a2b")
    world.sites.pages[job.url + "/apply"] = lever_form(captcha=True)
    world.sites.pages[job.url] = LEVER_POSTING.format(jd=JD, apply=job.apply_url)
    await pipeline.apply_job(job)
    job = refreshed(job)
    assert job.status == JobStatus.MANUAL and "challenge" in job.status_reason
    assert db.source_state("lever").paused_until is not None
    assert any("hit a" in m for m in world.telegram) and world.sites.submissions == []
    assert app_for(job) is None


async def test_s8_dry_run_fills_but_never_submits(world):
    job = make_job("greenhouse", "Acme S8", "https://job-boards.greenhouse.io/acme-s8/jobs/8",
                   GH_URL.format(slug="acme-s8", token=8), ext="acme-s8:8")
    world.sites.pages[job.apply_url] = greenhouse_page()
    await pipeline.apply_job(job, dry_run=True)
    job = refreshed(job)
    # A dry-run of a queued job waits for your approval instead of rejoining the automatic queue.
    assert job.status == JobStatus.REVIEW and "approve" in job.status_reason
    assert world.sites.submissions == [] and app_for(job) is None


async def test_s9_email_security_code_after_submit(world):
    """Some Greenhouse boards ask for a code emailed to you before the application counts."""
    job = make_job("greenhouse", "Acme S9", "https://job-boards.greenhouse.io/acme-s9/jobs/9",
                   GH_URL.format(slug="acme-s9", token=9), ext="acme-s9:9")
    world.sites.pages[job.apply_url] = greenhouse_page(
        success="Enter the 8-character security code we sent to your email to submit your application.")
    await pipeline.apply_job(job)
    job = refreshed(job)
    report(job, world)
    assert job.status != JobStatus.APPLIED, "the application isn't finished until the code is entered"


def report(job: Job, world) -> None:
    print(f"\n[{job.source}] status={job.status} reason={job.status_reason!r}")
    print(f"submissions={world.sites.submissions}")

