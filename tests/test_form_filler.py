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
            browser = await pw.chromium.launch(channel="chrome", headless=True)
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
