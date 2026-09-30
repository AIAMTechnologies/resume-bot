import random
from datetime import datetime, timedelta

from resumebot.config import SourcePacing
from resumebot.engine.pacing import after_action, evaluate, gap_seconds

CFG = SourcePacing(daily_cap=12, weekend_cap=3, active_hours=(9, 18), min_gap_seconds=240,
                   max_gap_seconds=720, session_minutes=(25, 50), break_minutes=(20, 60))
WEEKDAY_NOON = datetime(2026, 9, 29, 12, 0)   # Tuesday
SATURDAY_NOON = datetime(2026, 10, 3, 12, 0)


def test_allows_inside_hours_under_cap():
    assert evaluate(CFG, WEEKDAY_NOON, 0, 0, 60).ok


def test_blocks_outside_active_hours_and_schedules_next_morning():
    d = evaluate(CFG, WEEKDAY_NOON.replace(hour=21), 0, 0, 60)
    assert not d.ok and d.reason == "outside active hours"
    assert d.retry_at.date() == (WEEKDAY_NOON + timedelta(days=1)).date() and d.retry_at.hour == 9


def test_daily_and_weekend_caps():
    assert not evaluate(CFG, WEEKDAY_NOON, 12, 12, 60).ok
    assert evaluate(CFG, WEEKDAY_NOON, 11, 11, 60).ok
    assert not evaluate(CFG, SATURDAY_NOON, 3, 3, 60).ok


def test_global_cap():
    assert not evaluate(CFG, WEEKDAY_NOON, 0, 60, 60).ok


def test_cooldown_and_gap():
    assert not evaluate(CFG, WEEKDAY_NOON, 0, 0, 60, paused_until=WEEKDAY_NOON + timedelta(hours=1)).ok
    d = evaluate(CFG, WEEKDAY_NOON, 0, 0, 60, next_action_at=WEEKDAY_NOON + timedelta(minutes=3))
    assert not d.ok and d.reason.startswith("waiting")


def test_manual_and_disabled():
    assert not evaluate(SourcePacing(mode="manual"), WEEKDAY_NOON, 0, 0, 60).ok
    assert not evaluate(SourcePacing(enabled=False), WEEKDAY_NOON, 0, 0, 60).ok


def test_gaps_stay_in_range():
    rng = random.Random(1)
    gaps = [gap_seconds(CFG, rng) for _ in range(500)]
    assert min(gaps) >= 240 and max(gaps) <= 720
    assert len({round(g) for g in gaps}) > 100  # not robotic


def test_session_break_cycle():
    rng = random.Random(2)
    nxt, sess_end, brk = after_action(CFG, WEEKDAY_NOON, None, rng)
    assert not brk and 240 <= (nxt - WEEKDAY_NOON).total_seconds() <= 720
    nxt, _, brk = after_action(CFG, sess_end + timedelta(seconds=1), sess_end, rng)
    assert brk and (nxt - sess_end).total_seconds() >= 20 * 60
