"""Offline form-filler test: a page shaped like Ashby (autofill box + real form), headless Chrome."""
from pathlib import Path

import pytest

from resumebot.engine.questions import Field
from resumebot.sources.base import ApplyContext, Materials

PAGE = """<!doctype html><html><body>
<form id="autofill"><label for="af">Autofill from resume. Upload your resume here to autofill key application fields.</label>
  <input type="file" id="af"></form>
<form id="application">
  <label for="name">Name*</label><input id="name" required>
  <label for="email">Email*</label><input id="email" type="email" required>
  <label for="phone">Phone Number*</label><input id="phone" type="tel" required>
  <label for="resume">Resume*</label><input id="resume" type="file" required>
  <fieldset><legend>Are you legally authorized to work in Canada?*</legend>
    <label><input type="radio" name="auth" value="y">Yes</label><label><input type="radio" name="auth" value="n">No</label>
  </fieldset>
  <label for="why">Why are you interested in this role?</label><textarea id="why"></textarea>
  <label><input type="checkbox" id="consent" required> I consent to the processing of my data per the privacy policy</label>
</form></body></html>"""


class FastHuman:
    """Same interface as browser.human.Human without the human-speed delays."""

    def __init__(self, page):
        self.page = page

    async def pause(self, a=0, b=0):
        pass

    async def click(self, loc):
        await loc.click()

    async def type(self, loc, text, clear=True, typos=True):
        await loc.fill(text)

    async def select(self, loc, label):
        await loc.select_option(label=label)

    async def check(self, loc):
        await loc.check()

    async def upload(self, loc, path):
        await loc.set_input_files(path)


async def _launch(pw):
    try:
        return await pw.chromium.launch(channel="chrome", headless=True)
    except Exception:  # Google Chrome not installed; Playwright's own Chromium is fine for this page
        return await pw.chromium.launch(headless=True)


@pytest.fixture
def chrome_page():
    pw_api = pytest.importorskip("playwright.async_api")
    return pw_api


async def test_fills_real_form_and_skips_autofill(tmp_path, monkeypatch, chrome_page):
    from resumebot.sources import forms
    monkeypatch.setattr(forms, "answers", lambda: {"application_consent": True})
    resume = tmp_path / "Ammar_Alam_Resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    page_file = tmp_path / "form.html"
    page_file.write_text(PAGE)

    answers_given = {"Name": "Ammar Alam", "Email": "a@example.com", "Phone Number": "647-555-0100",
                     "Are you legally authorized to work in Canada?": "Yes"}

    async def answer(f: Field) -> str:
        key = f.label.rstrip("*").strip()
        return answers_given.get(key, "")

    try:
        async with chrome_page.async_playwright() as pw:
            browser = await _launch(pw)
            page = await browser.new_page()
            await page.goto(page_file.as_uri())
            ctx = ApplyContext(job=None, page=page, human=FastHuman(page),
                               materials=Materials(resume, resume), answer=answer)
            filled = await forms.fill_form(ctx, "form")
            vals = await page.evaluate("""() => ({
                name: document.getElementById('name').value, email: document.getElementById('email').value,
                phone: document.getElementById('phone').value,
                resume: document.getElementById('resume').files.length, af: document.getElementById('af').files.length,
                auth: (document.querySelector('input[name=auth]:checked') || {}).value,
                consent: document.getElementById('consent').checked })""")
            await browser.close()
    except Exception as e:  # Chrome not installed on this machine
        if "Executable" in str(e) or "channel" in str(e):
            pytest.skip(f"Chrome unavailable: {e}")
        raise

    assert vals == {"name": "Ammar Alam", "email": "a@example.com", "phone": "647-555-0100",
                    "resume": 1, "af": 0, "auth": "y", "consent": True}, vals
    assert "Name*" in filled or "Name" in " ".join(filled)


ASHBY_CONTROLS = '''<form>
<label class=some_option>Prefer not to say</label>
<div class="ashby-application-form-field-entry">
<label class="_required_abc">Location</label>
<input role="combobox" aria-autocomplete="list" oninput="document.querySelector('[role=listbox]').hidden=false">
<div role="listbox" hidden><div role="option" onclick="document.querySelector('input').value=this.innerText;this.parentNode.hidden=true">Toronto, Ontario, Canada</div></div>
</div>
<div class="ashby-application-form-field-entry">
<label class="_required_abc">Can you attend Anchor Days?</label>
<div class="ashby-application-form-input-yesno">
<button type="button" class="ashby-application-form-input-yesno-option" data-option="yes" aria-pressed="false" onclick="this.setAttribute('aria-pressed','true')">Yes</button>
<button type="button" class="ashby-application-form-input-yesno-option" data-option="no" aria-pressed="false" onclick="this.setAttribute('aria-pressed','true')">No</button>
<input type="checkbox" style="display:none">
</div></div></form>'''


@pytest.mark.parametrize('unknown', [False, True])
async def test_ashby_required_location_and_button_questions(tmp_path, chrome_page, unknown):
    from resumebot.sources import forms
    from resumebot.sources.base import NeedsInput
    from resumebot.engine.questions import NeedsHuman
    seen = []
    async def answer(f):
        seen.append(f)
        if f.label == 'Location':
            assert not f.options  # unrelated Yes/No buttons are not location choices
            return 'Toronto'
        if unknown:
            raise NeedsHuman(f.label, '', f.options)
        return 'No'
    async with chrome_page.async_playwright() as pw:
        browser = await pw.chromium.launch(channel='chrome', headless=True)
        page = await browser.new_page()
        await page.set_content(ASHBY_CONTROLS)
        ctx = ApplyContext(job=None, page=page, human=FastHuman(page),
                           materials=Materials(tmp_path/'unused', tmp_path/'unused'), answer=answer)
        if unknown:
            with pytest.raises(NeedsInput) as exc:
                await forms.fill_form(ctx, 'form')
            assert exc.value.questions == [('Can you attend Anchor Days?', '', ['Yes', 'No'])]
            assert await page.locator('[aria-pressed=true]').count() == 0
        else:
            await forms.fill_form(ctx, 'form')
            assert await page.locator('[data-option=no]').get_attribute('aria-pressed') == 'true'
        assert await page.locator('input[role=combobox]').input_value() == 'Toronto, Ontario, Canada'
        assert [f.required for f in seen] == [True, True]
        await browser.close()


async def test_unmatched_combobox_does_not_press_enter(chrome_page):
    from types import SimpleNamespace
    from resumebot.sources import forms
    async with chrome_page.async_playwright() as pw:
        browser = await pw.chromium.launch(channel='chrome', headless=True)
        page = await browser.new_page()
        await page.set_content('<form onsubmit="window.submitted=true;return false"><input role="combobox"></form>')
        ctx = SimpleNamespace(page=page, human=FastHuman(page))
        assert not await forms._pick_combobox(ctx, page.locator('input'), 'Missing city')
        assert not await page.evaluate('Boolean(window.submitted)')
        await browser.close()


async def test_fills_fields_after_reactive_rerender(tmp_path, chrome_page):
    """A React-style redraw removes temporary scan attributes after each edit."""
    from resumebot.sources import forms
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    async def answer(field):
        return {"First": "Ammar", "Last": "Alam"}.get(field.label, "")
    async with chrome_page.async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page()
        await page.set_content('''<form><label for="first">First</label><input id="first"
          oninput="document.querySelectorAll('[data-rb-id]').forEach(e => e.removeAttribute('data-rb-id'))">
          <label for="last">Last</label><input id="last"></form>''')
        ctx = ApplyContext(job=None, page=page, human=FastHuman(page), materials=Materials(resume, resume), answer=answer)
        await forms.fill_form(ctx, "form")
        assert await page.locator("#first").input_value() == "Ammar"
        assert await page.locator("#last").input_value() == "Alam"
        await browser.close()


CONSENT_PAGE = """<!doctype html><html><body><form id="application">
  <label for="country">Country*</label>
  <select id="country" required><option value="0">Please select</option><option value="ca">Canada</option></select>
  <label><input type="checkbox" id="consent" required> I agree to the privacy policy and processing of my data</label>
</form></body></html>"""


async def test_consent_uses_your_answer_and_placeholder_select_is_filled(tmp_path, monkeypatch, chrome_page):
    from resumebot.engine import questions
    from resumebot.sources import forms
    monkeypatch.setattr(forms, "answers", lambda: {"application_consent": False})
    questions.remember("I agree to the privacy policy and processing of my data", "check")
    page_file = tmp_path / "form.html"
    page_file.write_text(CONSENT_PAGE)
    asked = []

    async def answer(f: Field) -> str:
        asked.append(f.label)
        return "Canada" if f.label.startswith("Country") else ""

    try:
        async with chrome_page.async_playwright() as pw:
            browser = await _launch(pw)
            page = await browser.new_page()
            await page.goto(page_file.as_uri())
            ctx = ApplyContext(job=None, page=page, human=FastHuman(page),
                               materials=Materials(tmp_path / "r.pdf", tmp_path / "r.docx"), answer=answer)
            await forms.fill_form(ctx, "form")
            vals = await page.evaluate("""() => ({country: document.getElementById('country').value,
                consent: document.getElementById('consent').checked})""")
            await browser.close()
    except Exception as e:
        if "Executable" in str(e):
            pytest.skip(f"Chromium unavailable: {e}")
        raise

    assert vals == {"country": "ca", "consent": True}, vals
    assert any(a.startswith("Country") for a in asked)
