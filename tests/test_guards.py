"""Challenge detection must not fire on security job descriptions."""
import pytest

from resumebot.browser import guards
from resumebot.browser.guards import challenge_kind

SOC_JD = ("As a SOC Analyst you will investigate unusual activity and suspicious activity across endpoints, "
          "run security verification of controls, and verify that you are able to triage alerts. ") * 60


def test_long_job_description_is_not_a_challenge():
    assert challenge_kind("Security Analyst | Apply for this job", SOC_JD, len(SOC_JD)) is None


def test_headline_or_short_page_is_a_challenge():
    assert challenge_kind("Verify you are human", "", 20) == "human verification"
    assert challenge_kind("Indeed", "We've detected unusual activity from your network.", 60) == "unusual activity warning"
    assert challenge_kind("Let's do a quick security check | Indeed", SOC_JD, len(SOC_JD)) == "security check"


async def test_check_page_in_browser():
    pw_api = pytest.importorskip("playwright.async_api")
    async with pw_api.async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True, channel="chrome")
        except Exception:  # noqa: BLE001 — no Google Chrome here; use any bundled Chromium
            import os
            from pathlib import Path
            exe = os.environ.get("RESUMEBOT_TEST_CHROMIUM") or (
                "/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None)
            try:
                browser = await pw.chromium.launch(headless=True, executable_path=exe)
            except Exception as e:  # noqa: BLE001
                pytest.skip(f"Chromium unavailable: {e}")
        page = await browser.new_page()
        await page.set_content(f"<h1>SOC Analyst</h1><div>{SOC_JD}</div><form><input></form>")
        await guards.check_page(page)  # no exception
        await page.set_content("<h1>Verify you are human</h1><p>Complete the check below.</p>")
        with pytest.raises(guards.ChallengeDetected) as exc:
            await guards.check_page(page)
        assert exc.value.kind == "human verification"
        await browser.close()
