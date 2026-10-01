import pytest
from resumebot.engine import matcher
from resumebot.models import Job
from resumebot.sources.routing import route


def test_route_ats_urls():
    assert route("https://boards.greenhouse.io/stripe/jobs/123456").source == "greenhouse"
    r = route("https://job-boards.greenhouse.io/embed/job_app?for=figma&token=555")
    assert (r.source, r.slug, r.job_id) == ("greenhouse", "figma", "555")
    r = route("https://jobs.lever.co/koho/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b/apply")
    assert (r.source, r.slug) == ("lever", "koho")
    assert route("https://jobs.ashbyhq.com/ramp/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b").source == "ashby"
    assert route("https://acme.wd5.myworkdayjobs.com/en-US/careers/job/Toronto/Engineer_R123").source == "workday"
    assert route("https://careers.example.com/jobs/1").source == "external"


def test_dedupe_key_ignores_noise():
    assert matcher.dedupe_key("Acme Inc", "Backend Engineer (Remote)") == matcher.dedupe_key("acme inc.", "Backend Engineer")


def test_title_match():
    targets = ["Backend Engineer", "Software Engineer"]
    assert matcher.title_ok("Senior Software Engineer, Payments", targets)
    assert not matcher.title_ok("Account Executive", targets)


def test_location_rules():
    ok = lambda loc, remote=False: matcher.location_ok(Job(source="x", external_id="1", company="c", title="t",  # noqa: E731
                                                           url="u", location=loc, remote=remote))[0]
    assert ok("Toronto, ON")
    assert ok("Remote - Canada", True)
    assert ok("Remote (US)", True)
    assert not ok("Remote - Germany", True)
    assert not ok("Remote - Australia", True)
    assert not ok("Remote (South Africa)", True)
    assert not ok("Remote - Russia", True)
    assert not ok("London, UK")


def test_us_locations_without_country():
    from resumebot.engine.geography import is_us_location
    assert is_us_location('San Francisco, California')
    assert is_us_location('Austin, TX')
    assert is_us_location('New York, NY')
    assert not is_us_location('Toronto, ON')
    assert not is_us_location('Tbilisi, Georgia country')
    assert not is_us_location('Georgia')


def test_us_relocation_filter(monkeypatch):
    from types import SimpleNamespace
    from resumebot.engine import matcher
    from resumebot.models import Job
    loc = SimpleNamespace(remote=True, remote_regions=[], cities=[], countries=['United States'])
    monkeypatch.setattr(matcher, 'settings', lambda: SimpleNamespace(targets=SimpleNamespace(locations=loc)))
    for location in ['Austin, TX', 'San Francisco, California', 'New York, NY', 'Chicago, IL']:
        assert matcher.location_ok(Job(title='Security Engineer', location=location))[0]
    assert not matcher.location_ok(Job(title='Security Engineer', location='Paris, France'))[0]


def test_linkedin_offsite_job_found_on_company_ats_is_skipped():
    from resumebot.models import Job
    job = Job(source="linkedin", external_id="9", company="Acme", title="Security Analyst II", url="u",
              location="Toronto, ON", apply_url="https://job-boards.greenhouse.io/acme/jobs/1", easy_apply=False)
    ok, why = matcher.prefilter(job)
    assert not ok and "company ATS" in why


def test_manual_application_counts_once_toward_company_cap():
    from resumebot import db
    from resumebot.engine import pipeline, review
    from resumebot.models import Job, ReviewItem
    job = db.save(Job(source="external", external_id="cap-1", company="CapCo Inc", title="Analyst", url="u"))
    item = db.save(ReviewItem(job_id=job.id, kind="manual"))
    assert pipeline.company_recent_count("CapCo Inc") == 1
    review.resolve(item.id, "done")
    assert pipeline.company_recent_count("CapCo Inc") == 1


def test_remote_jobs_tied_to_other_countries_are_rejected():
    def ok(loc):
        return matcher.location_ok(Job(source="x", external_id="1", company="c", title="t", url="u",
                                       location=loc, remote=True))[0]
    assert not ok("London, UK")
    assert not ok("Bengaluru, India")
    assert ok("Remote (United States | Canada)")
    assert ok("Ontario - Remote")
    assert ok("San Francisco")
    assert ok("US - Remote")
    assert ok("Remote")


def test_queued_job_rechecked_before_applying():
    from resumebot import db
    from resumebot.engine import pipeline
    from resumebot.models import Job, JobStatus
    from uuid import uuid4
    bad = db.save(Job(source="ashby", external_id=uuid4().hex, company="Far Co", title="Security Analyst", url="u",
                      location="London, UK", remote=True, status=JobStatus.QUEUED, match_score=99))
    picked = pipeline.next_job("ashby")
    assert picked is None or picked.id != bad.id
    with db.session() as s:
        assert s.get(Job, bad.id).status == JobStatus.SKIPPED


def test_queued_job_at_an_excluded_company_is_never_applied(monkeypatch):
    from resumebot import db
    from resumebot.config import settings
    from resumebot.engine import pipeline
    from resumebot.models import Job, JobStatus
    from uuid import uuid4
    monkeypatch.setattr(settings().targets, "exclude_companies", ["Cohere"])
    job = db.save(Job(source="lever", external_id=uuid4().hex, company="Cohere", title="Security Engineer", url="u",
                      location="Toronto, ON", status=JobStatus.QUEUED, match_score=99))
    picked = pipeline.next_job("lever")
    assert picked is None or picked.id != job.id
    with db.session() as s:
        assert s.get(Job, job.id).status == JobStatus.SKIPPED



@pytest.mark.parametrize("title,ok", [
    ("Senior Security Engineer - Detection & Response", True), ("Staff CSIRT Analyst", True),
    ("SOC Support Specialist", True), ("Vulnerability Management Engineer", True), ("Senior AI Engineer", True),
    ("Sales Engineer 2 (Customer Success)", False), ("Implementation Consultant I, Commercial", False),
    ("Software Engineer I, Frontend", False), ("Account Associate - EMEA", False), ("IT Systems Engineer", False),
])
def test_title_filter_needs_a_meaningful_match(title, ok):
    targets = ["Senior Information Security Analyst", "Security Operations Manager", "Incident Response Lead",
               "Security Engineer", "Cloud Security Engineer", "GRC Analyst", "AI Automation Engineer"]
    keywords = ["security", "cyber", "soc", "incident", "threat", "iam", "identity", "grc", "siem", "dfir",
                "infosec", "detection", "vulnerability", "risk", "automation", "csirt"]
    assert matcher.title_ok(title, targets, keywords) is ok
