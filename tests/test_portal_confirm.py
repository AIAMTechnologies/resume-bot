"""Portal submit outcomes: company-worded confirmations, human checks handed to you, closed postings."""
import pytest

from resumebot.models import Job
from resumebot.sources import ats_boards
from resumebot.sources.base import ApplyContext, Materials, PostingClosed
from tests.test_form_filler import FastHuman, _launch

ASHBY_DONE = """<button onclick="document.body.innerHTML =
  '<div class=ashby-application-form-success-container><h2>Success</h2><p>Thanks, got it!</p></div>'">Submit Application</button>"""
# hCaptcha pops a picture puzzle after Submit; the form goes through once a person solves it.
CAPTCHA_THEN_DONE = """<button onclick="document.body.insertAdjacentHTML('beforeend',
  '<iframe title=\\'Main content of the hCaptcha challenge\\' width=300 height=300></iframe>');
  setTimeout(() => document.body.innerHTML = 'Application submitted!', 4000)">Submit Application</button>"""
CLOSED = "<h1>Current openings at Stripe</h1><p>0 jobs</p>"


async def _ctx(pw_api, tmp_path, html):
    pw = await pw_api.async_playwright().start()
    try:
        browser = await _launch(pw)
    except Exception as e:  # Chrome not installed on this machine
        await pw.stop()
        pytest.skip(f"Chrome unavailable: {e}")
    page = await browser.new_page()
    (tmp_path / "page.html").write_text(f"<!doctype html><html><body>{html}</body></html>")
    job = Job(source="ashby", external_id="x:1", company="Writer", title="Security engineer", url=(tmp_path / "page.html").as_uri())
    resume = tmp_path / "r.pdf"
    resume.write_bytes(b"%PDF-1.4")
    return pw, ApplyContext(job=job, page=page, human=FastHuman(page), materials=Materials(resume, resume), answer=None)


@pytest.fixture
def pw_api():
    return pytest.importorskip("playwright.async_api")


async def test_ashby_success_banner_with_custom_wording_counts_as_submitted(tmp_path, pw_api):
    pw, ctx = await _ctx(pw_api, tmp_path, ASHBY_DONE)
    try:
        await ctx.page.goto(ctx.job.url)
        res = await ats_boards.Ashby()._submit_and_confirm(ctx, ["submit application"], r"application submitted")
    finally:
        await pw.stop()
    assert res.submitted, res.note


async def test_captcha_after_submit_is_handed_to_you_not_solved(tmp_path, pw_api, monkeypatch):
    pings = []

    async def send(text, *a, **k):
        pings.append(text)
    monkeypatch.setattr("resumebot.notify.telegram.send", send)
    waits = iter([2, 30])  # short first wait so the test doesn't sit through the normal 25 s
    confirmed = ats_boards._BoardSource._confirmed
    monkeypatch.setattr(ats_boards._BoardSource, "_confirmed",
                        lambda self, ctx, success, timeout: confirmed(self, ctx, success, next(waits)))
    pw, ctx = await _ctx(pw_api, tmp_path, CAPTCHA_THEN_DONE)
    try:
        await ctx.page.goto(ctx.job.url)
        res = await ats_boards.Lever()._submit_and_confirm(ctx, ["submit application"], r"application submitted")
    finally:
        await pw.stop()
    assert res.submitted and "human check" in res.note, res.note
    assert len(pings) == 1 and "CAPTCHA" in pings[0]


async def test_closed_posting_is_not_a_failed_application(tmp_path, pw_api):
    pw, ctx = await _ctx(pw_api, tmp_path, CLOSED)

    async def read(_):
        raise AssertionError("should not read a closed posting")
    ctx.human.read = read
    try:
        with pytest.raises(PostingClosed):
            await ats_boards.Greenhouse()._open_and_read(ctx, ctx.job.url)
    finally:
        await pw.stop()


CONSENT_GROUP = """<form><fieldset><legend>Applicant Privacy Statement *</legend>
  <label><input type="checkbox" name="q1" value="a">By clicking this box and submitting your application, you consent to our Applicant Privacy Statement.</label>
  <label><input type="checkbox" name="q1" value="b">By clicking this box and submitting your application, you consent to Strider Technologies conducting third-party background checks and utilizing advanced tools to assess potential risks as part of your application process.</label>
</fieldset></form>"""


@pytest.mark.parametrize("background_ok", [True, False])
async def test_group_of_consent_boxes_follows_your_answers_not_the_ai(tmp_path, pw_api, monkeypatch, background_ok):
    from resumebot.engine import questions
    from resumebot.engine.questions import NeedsHuman
    from resumebot.sources import forms
    from resumebot.sources.base import NeedsInput
    monkeypatch.setattr(forms, "answers", lambda: {"application_consent": True})
    monkeypatch.setattr(questions, "answers", lambda: {
        "screening": {"third_party_background_screening": "Yes" if background_ok else ""}})

    async def answer(f):
        raise NeedsHuman(f.label, "", f.options)
    pw, ctx = await _ctx(pw_api, tmp_path, CONSENT_GROUP)
    ctx.answer = answer
    try:
        await ctx.page.goto(ctx.job.url)
        if background_ok:
            await forms.fill_form(ctx, "form")
        else:  # no answer on file for background checks: you're asked, nothing is ticked for you
            with pytest.raises(NeedsInput):
                await forms.fill_form(ctx, "form")
        ticked = await ctx.page.evaluate("[...document.querySelectorAll('input')].map(e => e.checked)")
    finally:
        await pw.stop()
    assert ticked == ([True, True] if background_ok else [False, False])


async def test_radio_without_ids_clicks_the_chosen_option_not_the_first(tmp_path, pw_api):
    """Lever radios have a shared name and no id."""
    from resumebot.sources import forms
    pw, ctx = await _ctx(pw_api, tmp_path, """<form><fieldset><legend>Will you require sponsorship?*</legend>
      <label><input type="radio" name="cards[a][field0]" value="Yes">Yes</label>
      <label><input type="radio" name="cards[a][field0]" value="No">No</label></fieldset></form>""")

    async def answer(f):
        return "No"
    ctx.answer = answer
    try:
        await ctx.page.goto(ctx.job.url)
        await forms.fill_form(ctx, "form")
        picked = await ctx.page.evaluate("document.querySelector('input:checked').value")
    finally:
        await pw.stop()
    assert picked == "No"


async def test_answer_wiped_by_a_late_rerender_is_typed_back_before_submit(tmp_path, pw_api):
    from resumebot.sources import forms
    # The form blanks the phone box shortly after it was typed (a resume parser finishing late).
    pw, ctx = await _ctx(pw_api, tmp_path, """<form><label for="p">Phone Number</label>
      <input id="p" type="tel" required onchange="setTimeout(() => { if (!window.done) { this.value = ''; window.done = 1; } }, 50)">
      <label for="n">Name</label><input id="n" required></form>""")

    async def answer(f):
        return "416-555-0100" if "Phone" in f.label else "Ada Lovelace"
    ctx.answer = answer
    try:
        await ctx.page.goto(ctx.job.url)
        await forms.fill_form(ctx, "form")
        phone = await ctx.page.input_value("#p")
    finally:
        await pw.stop()
    assert phone == "416-555-0100"


async def test_disabled_hosted_page_becomes_a_manual_card(tmp_path, pw_api):
    from resumebot.sources.base import ManualRequired
    pw, ctx = await _ctx(pw_api, tmp_path, "<h1>Page not found</h1><p>The page you requested was not found</p>")
    try:
        with pytest.raises(ManualRequired):
            await ats_boards.Ashby()._open_and_read(ctx, ctx.job.url)
    finally:
        await pw.stop()


ASHBY_EDUCATION = """<form><div class="ashby-application-form-field-entry" data-field-path="_systemfield_education_history">
<label>Education History</label>
<div class="ashby-application-form-input-education-entry">
  <div><label for="edu-school">School</label><div>
    <input role="combobox" aria-autocomplete="list" placeholder="Search schools..."
      oninput="document.querySelector('[role=listbox]').hidden = !this.value"></div></div>
  <div role="listbox" hidden>
    <div role="option" onclick="pick(this)"><div><span>University of Toledo</span></div><div>United States</div><div>utoledo.edu</div></div>
    <div role="option" onclick="pick(this)"><div><span>University of Toronto</span></div><div>Canada</div><div>utoronto.ca</div></div>
  </div>
  <div><label for="edu-start">Start Date</label><div id="edu-start">
    <select><option disabled hidden value="" selected>Month...</option><option value="9">September</option></select>
    <select><option disabled hidden value="" selected>Year...</option><option value="2014">2014</option></select></div></div>
</div></div>
<script>function pick(o) { const i = document.querySelector('input'); i.value = o.querySelector('span').innerText;
  i.dataset.picked = o.innerText; o.parentNode.hidden = true; }</script></form>"""


@pytest.mark.parametrize("start", ["", "2014-09"])
async def test_ashby_education_block_school_search_and_study_dates(tmp_path, pw_api, monkeypatch, start):
    from resumebot.engine import questions
    from resumebot.sources import forms
    monkeypatch.setattr(forms, "answers", lambda: {})
    monkeypatch.setattr(questions, "answers", lambda: {
        "contact": {"province_state": "ON", "country": "Canada"},
        "logistics": {"earliest_start": "2 weeks from offer"},
        "education": {"school": "University of Toronto", "start_date": start}})

    async def no_llm(*a, **k):
        raise AssertionError("education fields are never sent to the AI")
    monkeypatch.setattr(questions, "from_llm", no_llm)
    pw, ctx = await _ctx(pw_api, tmp_path, ASHBY_EDUCATION)
    ctx.answer = questions.Answerer("Security Engineer", "Plaid", "", "Toronto, ON", source="ashby")
    ctx.page.set_default_timeout(3000)
    try:
        await ctx.page.goto(ctx.job.url)
        await forms.fill_form(ctx, "form")
        got = await ctx.page.evaluate("""() => ({picked: document.querySelector('input').dataset.picked || '',
            dates: [...document.querySelectorAll('select')].map(s => s.value)})""")
    finally:
        await pw.stop()
    assert "Canada" in got["picked"], got           # the Toronto school, chosen from a three-line option
    assert got["dates"] == (["9", "2014"] if start else ["", ""]), got  # never "2 weeks from offer"
