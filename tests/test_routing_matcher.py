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
