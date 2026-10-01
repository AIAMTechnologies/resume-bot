"""Emails tab: every inbox email is stored and linked to its application; KPI tiles open lists."""
from uuid import uuid4

import httpx

from resumebot import db
from resumebot.engine import stats
from resumebot.inbox import gmail
from resumebot.models import Application, AppStatus, Email, Job, utcnow
from resumebot.web.app import app


async def test_inbox_emails_are_stored_and_linked(monkeypatch):
    company = f"Zeta{uuid4().hex[:6]}"
    job = db.save(Job(source="greenhouse", external_id=uuid4().hex, company=company, title="Security Analyst", url="u"))
    a = db.save(Application(job_id=job.id, source="greenhouse", company=company, title="Security Analyst",
                            submitted_at=utcnow(), status=AppStatus.SUBMITTED))
    body = f"Thank you for your interest in {company}. Unfortunately we will not be moving forward."
    await gmail._process(f"Talent <jobs@{company.lower()}.com>", "Your application", body, key=f"k-{company}")
    await gmail._process(f"Talent <jobs@{company.lower()}.com>", "Your application", body, key=f"k-{company}")  # repeat
    await gmail._process("News <news@example.com>", "Weekly digest", "nothing", key=f"n-{company}")
    rows = [e for e in stats.emails(q=company)]
    assert len(rows) == 1 and rows[0].application_id == a.id and rows[0].classification == AppStatus.REJECTED
    with db.session() as s:
        assert s.get(Application, a.id).status == AppStatus.REJECTED
        assert s.exec(db.select(Email).where(Email.message_key == f"n-{company}")).one().application_id is None
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    async with client:
        page = (await client.get("/emails")).text
        assert company in page and "❌ rejected" in page
        assert "Emails about this application" in (await client.get(f"/applications/{a.id}")).text
        today = (await client.get("/applications?when=today")).text
        assert "Applied today" in today and "Security Analyst" in today
        assert 'href="/applications?when=today"' in (await client.get("/partials/kpis")).text


async def test_application_receipt_with_future_rejection_boilerplate_is_confirmed():
    subject = "Thank you for applying to SentinelOne!"
    body = ("We received your application and look forward to reviewing it. "
            "If you are not selected for this position, we will keep your profile in our talent community.")
    status, _ = await gmail.classify(subject, body)
    assert status == AppStatus.CONFIRMED
    assert gmail._rule_class(subject, body) == AppStatus.CONFIRMED
