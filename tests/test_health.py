"""Error monitor: fingerprints, pattern bookkeeping, remedies at their thresholds, and the /errors card."""
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from sqlmodel import SQLModel, create_engine, delete

from resumebot import db
from resumebot.config import settings
from resumebot.engine import health, stats
from resumebot.engine.pacing import Gate
from resumebot.models import Application, AppStatus, Diagnostic, ErrorPattern, Job, utcnow
from resumebot.web.app import app

HEALTH_KV = ("health_last_scan", "skipped_boards", "manual_companies", "pacing_scale")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    with db.session() as s:
        s.exec(delete(Diagnostic))
        s.exec(delete(ErrorPattern))
        s.commit()
    for key in HEALTH_KV:
        db.kv_set(key, None)
    sent = []
    monkeypatch.setattr(health, "_notify", lambda note, status: sent.append((status, note)))
    yield sent


_clock = [utcnow()]


def add(message: str, kind: str = "system", level: str = "error", job_id: int | None = None, ts=None) -> Diagnostic:
    """A diagnostic row with a strictly increasing timestamp (scan() only reads rows after its watermark)."""
    _clock[0] = max(_clock[0], utcnow()) + timedelta(milliseconds=5)
    row = Diagnostic(id=uuid4().hex[:16], message=message, kind=kind, level=level, job_id=job_id, ts=ts or _clock[0])
    with db.session() as s:
        s.add(row)
        s.commit()
    return row


def pattern(key: str) -> ErrorPattern:
    with db.session() as s:
        return s.exec(db.select(ErrorPattern).where(ErrorPattern.key == key)).first()


# ---------------------------------------------------------------- fingerprint & classify


def test_fingerprint_collapses_volatile_parts():
    a = health.fingerprint("Timeout 30000ms on https://boards.greenhouse.io/acme/jobs/123 for job #42 'First Name'")
    b = health.fingerprint("Timeout 15000ms on https://jobs.lever.co/other/9 for job #7 'Last name'")
    assert a == b
    assert "URL" in a and "#" in a and "'…'" in a and "N" in a
    assert health.fingerprint("  deadbeefcafe1234 failed  ") == "ID failed"
    assert len(health.fingerprint("x" * 500)) == 160


def test_classify_known_shapes_and_entities():
    def key(message, kind="system", job_id=None):
        return health.classify(Diagnostic(id="x", message=message, kind=kind, job_id=job_id))

    remedy, k = key("greenhouse board 'acme' returned 404 — check the slug in config/companies.yaml", "discover")
    assert remedy.key == "board_dead" and k == "board_dead:greenhouse:acme"
    remedy, k = key("lever board 'acme' failed: boom", "discover")
    assert remedy.key == "board_error" and k == "board_error:lever:acme"
    remedy, k = key("Not submitted: Analyst @ Acme Corp — required field missing", "apply")
    assert remedy.key == "company_form" and k == "company_form:Acme Corp"
    remedy, k = key("⚠️ captcha — pausing linkedin until 2026-10-01 10:00 UTC", "challenge")
    assert remedy.key == "challenge" and k == "challenge:linkedin"
    remedy, k = key("Claude Code CLI not found on PATH")
    assert remedy.key == "cli_missing" and k == "cli_missing"
    # kind filters: a board-looking message outside discovery is not a dead board
    assert key("greenhouse board 'acme' returned 404", "apply")[0] is None
    remedy, k = key("Something odd happened 12 times")
    assert remedy is None and k == "unknown:something odd happened N times"


def test_classify_company_falls_back_to_job(monkeypatch):
    with db.session() as s:
        job = Job(source="greenhouse", external_id=uuid4().hex, company="Globex", title="Analyst", url="https://x")
        s.add(job)
        s.commit()
        s.refresh(job)
    _, k = health.classify(Diagnostic(id="x", message="Apply error: form timed out", kind="apply", job_id=job.id))
    assert k == "company_form:Globex"


# ---------------------------------------------------------------- scan bookkeeping


def test_scan_creates_and_increments_patterns():
    add("Something odd happened 1 times")
    changed = health.scan()
    assert [p.count for p in changed] == [1]
    assert health.scan() == []  # nothing new since the watermark
    add("Something odd happened 2 times")
    add("info lines are ignored", level="info")
    changed = health.scan()
    p = pattern("unknown:something odd happened N times")
    assert len(changed) == 1 and p.count == 2 and p.status == "watching"
    assert p.example == "Something odd happened 2 times"


def test_scan_keeps_last_five_job_ids():
    for job_id in range(1, 8):
        add("Something odd with a job", job_id=job_id)
    health.scan()
    assert pattern("unknown:something odd with a job").job_ids == [3, 4, 5, 6, 7]


# ---------------------------------------------------------------- remedies


def test_dead_board_skipped_at_threshold(clean):
    add("greenhouse board 'acme' returned 404 — check the slug", kind="discover", level="warning")
    health.scan()
    assert not health.board_skipped("greenhouse", "acme")
    assert pattern("board_dead:greenhouse:acme").status == "watching"
    add("greenhouse board 'acme' returned 404 — check the slug", kind="discover", level="warning")
    health.scan()
    p = pattern("board_dead:greenhouse:acme")
    assert health.board_skipped("greenhouse", "acme")
    assert not health.board_skipped("lever", "acme")
    assert p.status == "auto" and "Skipping board 'acme'" in p.action
    assert clean == [("auto", p.action)]


def test_board_remedy_without_source_does_not_crash():
    for _ in range(2):
        add("Board 'acme' returned 404", kind="discover", level="warning")
    health.scan()
    assert pattern("board_dead::acme").action.startswith("remedy failed")


def test_repeated_company_failures_route_to_manual():
    add("Not submitted: Analyst @ Initech — upload failed", kind="apply")
    health.scan()
    assert health.company_manual("Initech") == ""
    add("Not submitted: Engineer @ Initech — upload failed", kind="apply")
    health.scan()
    assert "2 automatic attempts failed" in health.company_manual("initech")
    assert health.company_manual("Other Co") == ""
    assert pattern("company_form:Initech").status == "auto"


def test_two_challenges_slow_the_source_down():
    source = "greenhouse"
    base = settings().pacing.sources.get(source)
    assert base is not None, "settings must configure greenhouse pacing for this test"
    add(f"⚠️ captcha — pausing {source} until 2026-10-01 10:00 UTC", kind="challenge")
    health.scan()
    assert health.pacing_scale(source) == 1.0
    add(f"⚠️ captcha — pausing {source} until 2026-10-02 10:00 UTC", kind="challenge")
    health.scan()
    assert health.pacing_scale(source) == 1.5
    assert health.pacing_scale("captcha") == 1.0
    cfg = Gate(source).cfg
    assert cfg.min_gap_seconds == int(base.min_gap_seconds * 1.5)
    assert cfg.max_gap_seconds == int(base.max_gap_seconds * 1.5)


def test_slow_down_is_capped():
    for _ in range(5):
        health.slow_down("indeed")
    assert health.pacing_scale("indeed") == 3.0


def test_remedy_state_expires():
    health.skip_board("ashby", "old", days=-1)
    health.skip_board("ashby", "new", days=1)
    assert not health.board_skipped("ashby", "old")
    assert health.board_skipped("ashby", "new")
    assert "ashby:old" not in db.kv_get("skipped_boards")


def test_needs_you_pattern_alerts_on_first_occurrence(clean):
    add("Claude Code CLI not found on PATH", kind="llm")
    health.scan()
    p = pattern("cli_missing")
    assert p.status == "needs-you" and p.alerted
    assert clean[0][0] == "needs-you" and "CLAUDE_CLI_PATH" in clean[0][1]
    assert [x.key for x in health.needs_you()] == ["cli_missing"]


def test_unknown_pattern_alerts_once_at_three(clean):
    for i in range(2):
        add(f"Weird thing number {i}")
    health.scan()
    assert clean == []
    add("Weird thing number 3")
    health.scan()
    p = pattern("unknown:weird thing number N")
    assert p.status == "needs-you" and p.alerted and len(clean) == 1
    assert "×3" in clean[0][1]
    add("Weird thing number 4")
    health.scan()
    assert len(clean) == 1


def test_resolve_reopens_on_recurrence(clean):
    for i in range(3):
        add(f"Weird thing number {i}")
    health.scan()
    p = pattern("unknown:weird thing number N")
    assert "Marked resolved" in health.resolve(p.id)
    assert health.resolve(999999) == "Unknown pattern."
    assert pattern(p.key).status == "resolved"
    assert p.key not in [x.key for x in health.patterns()]
    add("Weird thing number 9")
    health.scan()
    again = pattern(p.key)
    assert again.status == "watching" and again.count == 1 and not again.alerted


def test_summary_line():
    assert health.summary_line() == ""
    add("Claude Code CLI not found", kind="llm")
    for _ in range(2):
        add("greenhouse board 'acme' returned 410", kind="discover", level="warning")
    health.scan()
    line = health.summary_line()
    assert "1 problem(s) need you" in line and "1 auto-adjustment(s) active" in line


# ---------------------------------------------------------------- score bands


def test_score_bands_and_threshold_suggestion(monkeypatch, tmp_path):
    # score_bands() reads every submitted application, so give it a database of its own
    engine = create_engine(f"sqlite:///{tmp_path / 'bands.db'}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)
    with db.session() as s:
        def sent(score, status):
            job = Job(source="greenhouse", external_id=uuid4().hex, company="Band Co", title="Analyst",
                      url="https://x", match_score=score)
            s.add(job)
            s.commit()
            s.refresh(job)
            s.add(Application(job_id=job.id, source="greenhouse", company="Band Co", title="Analyst", submitted_at=utcnow(), status=status))
            s.commit()
        for i in range(10):
            sent(92, AppStatus.INTERVIEW if i < 3 else AppStatus.SUBMITTED)
        cfg = settings().matching
        low = next(lo for lo, _, _ in stats.SCORE_BANDS if lo >= cfg.review_score and lo < 90)
        for i in range(8):
            sent(low + 1, AppStatus.REJECTED if i < 2 else AppStatus.SUBMITTED)
        sent(None, AppStatus.INTERVIEW)  # unscored applications are left out
    result = stats.score_bands()
    bands = {b["band"]: b for b in result["bands"]}
    assert bands["90+"]["applied"] == 10 and bands["90+"]["positive"] == 3 and bands["90+"]["positive_rate"] == 30
    lowest = next(b for b in result["bands"] if b["low"] == low)
    assert lowest["applied"] == 8 and lowest["responses"] == 2 and lowest["positive"] == 0
    assert "consider raising matching.auto_apply_score" in result["suggestion"]
    assert result["auto_apply_score"] == cfg.auto_apply_score


# ---------------------------------------------------------------- dashboard


async def test_errors_page_shows_patterns_and_resolves():
    for i in range(3):
        add(f"Dashboard visible oddity {i}")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    async with client:
        page = await client.get("/errors")
        assert page.status_code == 200
        assert "Recurring problems" in page.text and "Dashboard visible oddity" in page.text
        p = pattern("unknown:dashboard visible oddity N")
        response = await client.post(f"/errors/patterns/{p.id}/resolve")
        assert response.status_code == 303 and response.headers["location"] == "/errors"
    assert pattern(p.key).status == "resolved"
