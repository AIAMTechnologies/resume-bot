from resumebot.models import ProfileItem
from resumebot.profile import master
from resumebot.resume import ats, render, tailor

CONTACT = {"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com", "phone": "+1 416 555 0100",
           "city": "Toronto", "province_state": "ON"}

RESUME = {
    "headline": "Backend Engineer",
    "summary": "Backend engineer building Python APIs and data pipelines on AWS.",
    "skills": [{"category": "Languages", "items": ["Python", "TypeScript", "SQL"]},
               {"category": "Cloud", "items": ["AWS Lambda", "PostgreSQL", "Docker"]}],
    "experience": [{"title": "Software Engineer", "organization": "Acme", "location": "Toronto, ON",
                    "start": "2022-01", "end": "Present",
                    "bullets": ["Built REST APIs in FastAPI serving 2M requests/day",
                                "Cut pipeline runtime 40% by moving batch jobs to AWS Lambda"]}],
    "projects": [{"title": "Resume Bot", "stack": ["Python", "Playwright"], "bullets": ["Automated job applications"]}],
    "education": [{"title": "BSc Computer Science", "organization": "University of Toronto", "end": "2021"}],
}


def test_coverage_uses_aliases():
    cov, hit, miss = ats.coverage("Experience with Postgres, k8s and JS", ["PostgreSQL", "Kubernetes", "JavaScript", "Rust"])
    assert set(hit) == {"PostgreSQL", "Kubernetes", "JavaScript"} and miss == ["Rust"]
    assert round(cov, 2) == 0.75


def test_render_roundtrip_is_ats_parseable(tmp_path):
    pdf, docx = tmp_path / "r.pdf", tmp_path / "r.docx"
    render.render_pdf(RESUME, CONTACT, pdf)
    render.render_docx(RESUME, CONTACT, docx)
    report = ats.check(pdf, docx, ["Python", "FastAPI", "AWS Lambda", "PostgreSQL", "Kafka"], CONTACT)
    assert report.ok, report.issues
    assert "Kafka" in report.missing and "FastAPI" in report.matched
    assert report.keyword_coverage == 0.8
    text = ats.parse_back(pdf).lower()
    for heading in ("experience", "education", "skills"):
        assert heading in text
    assert "ada@example.com" in text


def test_verify_strips_invented_claims():
    master.merge_items([{"kind": "experience", "title": "Software Engineer", "organization": "Acme",
                         "start": "2022-01", "end": "Present", "bullets": ["Built REST APIs serving 2M requests/day"],
                         "skills": ["Python", "FastAPI"]}], "test")
    src = next(i for i in master.all_items() if i.organization == "Acme")
    content = {
        "skills": [{"category": "Languages", "items": ["Python", "Rust"]}],
        "experience": [{"source_ids": [src.id], "title": "Staff Engineer", "organization": "Acme", "start": "2019",
                        "bullets": [{"text": "Built REST APIs serving 2M requests/day", "source_ids": [src.id]},
                                    {"text": "Led a team of 12 engineers", "source_ids": [src.id]}]}],
        "projects": [{"source_ids": [99999], "title": "Invented project", "bullets": []}],
    }
    dropped = tailor._verify(content, {i.id: i for i in master.all_items()})
    exp = content["experience"][0]
    assert exp["title"] == "Software Engineer" and exp["start"] == "2022-01"  # master record wins
    assert exp["bullets"] == ["Built REST APIs serving 2M requests/day"]     # invented "12" dropped
    assert content["skills"][0]["items"] == ["Python"]                        # Rust not in master
    assert content["projects"] == []
    assert any("Rust" in d for d in dropped)


def test_master_merge_dedupes():
    before = len(master.all_items())
    master.merge_items([{"kind": "project", "title": "Resume Bot", "bullets": ["Automates applications"],
                         "skills": ["Python"]}], "a")
    master.merge_items([{"kind": "project", "title": "Resume-Bot", "bullets": ["Automates applications.",
                                                                              "Has a dashboard"],
                         "skills": ["Python", "FastAPI"]}], "b")
    items = [i for i in master.all_items() if i.kind == "project" and "resume" in i.title.lower()]
    assert len(master.all_items()) == before + 1 and len(items) == 1
    assert items[0].bullets == ["Automates applications", "Has a dashboard"]
    assert set(items[0].sources) == {"a", "b"}


async def test_keyword_retry_keeps_best_draft_and_fits_two_pages(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from resumebot.resume import tailor as t

    def draft(coverage, pages):
        report = SimpleNamespace(keyword_coverage=coverage, missing=["Splunk"], score=80)
        return SimpleNamespace(content={"coverage": coverage}, pdf=tmp_path / "r.pdf", docx=tmp_path / "r.docx",
                               report=report, unsupported=[], jd_keywords=["Splunk"], pages=pages)

    drafts = iter([draft(0.4, 2), draft(0.3, 3)])  # first draft, then a worse, too-long retry

    async def fake_tailor(job, contact, extra_instruction=""):
        return next(drafts)

    trimmed = []

    def fake_trim(result, job, contact, max_pages=2):
        trimmed.append(result.report.keyword_coverage)
        result.pages = 2
        return result

    rendered = []
    monkeypatch.setattr(t, "tailor", fake_tailor)
    monkeypatch.setattr(t, "_pages", lambda r: r.pages)
    monkeypatch.setattr(t, "_trim_to_fit", fake_trim)
    monkeypatch.setattr(t.render, "render_pdf", lambda c, contact, p: rendered.append(c["coverage"]))
    monkeypatch.setattr(t.render, "render_docx", lambda c, contact, p: None)
    monkeypatch.setattr(t.ats, "check", lambda *a: SimpleNamespace(keyword_coverage=0.4, missing=[], score=80))
    monkeypatch.setattr(t, "settings", lambda: SimpleNamespace(
        ats=SimpleNamespace(min_keyword_coverage=0.7, retailor_attempts=1)))

    result = await t.tailor_with_retry(SimpleNamespace(), {})
    assert trimmed == [0.3]                       # the 3-page retry was trimmed to fit
    assert result.content == {"coverage": 0.4}    # but the better first draft is kept...
    assert rendered == [0.4]                      # ...and written back to disk
