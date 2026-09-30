from pathlib import Path

from resumebot import db
from resumebot.browser.session import BLOCKED_HOSTS
from resumebot.engine.pacing import Gate, prepared_today
from resumebot.models import Job, JobStatus, ReviewItem
from resumebot.sources import linkedin
from resumebot.sources.ats_locator import _location_ok, slug_candidates

FIX = Path(__file__).parent / "fixtures"


def test_parse_public_search_cards():
    cards = linkedin.parse_search((FIX / "linkedin_search.html").read_text())
    assert len(cards) == 10
    first = cards[0]
    assert first["id"] == "4471788215"
    assert first["title"] == "Information Security Analyst"
    assert first["company"] == "CAA Club Group"
    assert "Ontario" in first["location"] and first["posted"]


def test_parse_public_detail_detects_offsite():
    d = linkedin.parse_detail((FIX / "linkedin_job_offsite.html").read_text())
    assert len(d["description"]) > 1000
    assert d["offsite"] and not d["easy_apply"]
    assert d["employment_type"] == "Full-time"


def test_bot_browser_can_never_load_linkedin():
    for url in ["https://www.linkedin.com/jobs/view/1/", "https://ca.linkedin.com/in/x",
                "http://linkedin.com", "https://www.linkedin.com/login"]:
        assert BLOCKED_HOSTS.search(url), url
    for url in ["https://job-boards.greenhouse.io/embed/job_app?for=x", "https://notlinkedin.com.evil.io",
                "https://jobs.ashbyhq.com/linkedin-clone/123"]:
        assert not BLOCKED_HOSTS.search(url), url


def test_linkedin_apply_never_automates():
    import asyncio

    import pytest

    from resumebot.sources.base import ManualRequired
    with pytest.raises(ManualRequired):
        asyncio.run(linkedin.LinkedIn().apply(None))  # type: ignore[arg-type]


def test_slug_and_location_guards():
    assert "caaclubgroup" in slug_candidates("CAA Club Group")
    assert "1password" in slug_candidates("1Password")
    assert _location_ok("Toronto, ON", "Toronto, Ontario, Canada")
    assert _location_ok("Remote (Canada)", "Vaughan, Ontario, Canada")
    assert not _location_ok("San Francisco, CA", "Vaughan, Ontario, Canada")


def test_assist_cap_counts_prepared_cards():
    job = db.save(Job(source="linkedin", external_id="cap-test", company="C", title="T", url="u",
                      status=JobStatus.MANUAL))
    before = prepared_today("linkedin")
    db.save(ReviewItem(job_id=job.id, kind="manual"))
    assert prepared_today("linkedin") == before + 1
    # Gate uses prepared cards (not submissions) for assist-mode caps.
    g = Gate("linkedin")
    assert g.cfg.mode == "assist"
