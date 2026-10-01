"""Per-source pacing: caps, active hours, gaps, work sessions and breaks.

`evaluate` and `after_action` are pure functions (tested). `Gate` binds them to the DB.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlmodel import func, select

from .. import db
from ..config import SourcePacing, settings
from ..models import Application, SourceState, utcnow


@dataclass
class Decision:
    ok: bool
    reason: str = ""
    retry_at: datetime | None = None  # local time


def _next_active_start(cfg: SourcePacing, now: datetime) -> datetime:
    start_h, _ = cfg.active_hours
    candidate = now.replace(hour=start_h, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    # Spread the morning start so it isn't exactly on the hour every day.
    return candidate + timedelta(minutes=random.randint(3, 40))


def evaluate(cfg: SourcePacing, now: datetime, applied_today: int, global_today: int, global_cap: int,
             paused_until: datetime | None = None, next_action_at: datetime | None = None) -> Decision:
    """All datetimes are local-time naive. Returns whether the source may act now."""
    if not cfg.enabled:
        return Decision(False, "disabled")
    if cfg.mode == "manual":
        return Decision(False, "manual mode")
    if paused_until and now < paused_until:
        return Decision(False, "paused (cooldown)", paused_until)
    start_h, end_h = cfg.active_hours
    if not (start_h <= now.hour < end_h):
        return Decision(False, "outside active hours", _next_active_start(cfg, now))
    cap = cfg.weekend_cap if now.weekday() >= 5 else cfg.daily_cap
    if applied_today >= cap:
        return Decision(False, f"daily cap reached ({applied_today}/{cap})", _next_active_start(cfg, now))
    if global_today >= global_cap:
        return Decision(False, "global daily cap reached", _next_active_start(cfg, now))
    if next_action_at and now < next_action_at:
        return Decision(False, "waiting (human gap)", next_action_at)
    return Decision(True)


def gap_seconds(cfg: SourcePacing, rng: random.Random | None = None) -> float:
    rng = rng or random
    # Triangular skews toward shorter gaps with an occasional long one, like real browsing.
    lo, hi = cfg.min_gap_seconds, cfg.max_gap_seconds
    return rng.triangular(lo, hi, lo + (hi - lo) * 0.3)


def after_action(cfg: SourcePacing, now: datetime, session_ends_at: datetime | None,
                 rng: random.Random | None = None) -> tuple[datetime, datetime, bool]:
    """Returns (next_action_at, session_ends_at, took_break). Local-time naive."""
    rng = rng or random
    if session_ends_at is None:
        session_ends_at = now + timedelta(minutes=rng.uniform(*cfg.session_minutes))
    if now >= session_ends_at:
        brk = timedelta(minutes=rng.uniform(*cfg.break_minutes))
        next_at = now + brk
        return next_at, next_at + timedelta(minutes=rng.uniform(*cfg.session_minutes)), True
    return now + timedelta(seconds=gap_seconds(cfg, rng)), session_ends_at, False


# ---------- DB-bound ----------

def tz() -> ZoneInfo:
    return ZoneInfo(settings().timezone)


def local_now() -> datetime:
    return datetime.now(tz()).replace(tzinfo=None)


def to_local(dt: datetime | None) -> datetime | None:
    """Stored UTC (aware; naive treated as UTC) → local naive, for pacing maths and display."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz()).replace(tzinfo=None)


def to_utc(local_naive: datetime | None) -> datetime | None:
    """Local naive → aware UTC, for storage."""
    if local_naive is None:
        return None
    return local_naive.replace(tzinfo=tz()).astimezone(timezone.utc)


def local_day_start_utc() -> datetime:
    start = local_now().replace(hour=0, minute=0, second=0, microsecond=0)
    return to_utc(start)


def applied_today(source: str | None = None) -> int:
    with db.session() as s:
        q = select(func.count()).select_from(Application).where(
            Application.submitted_at >= local_day_start_utc())
        if source:
            q = q.where(Application.source == source)
        return s.exec(q).one()


def prepared_today(source: str) -> int:
    from ..models import Job, ReviewItem
    with db.session() as s:
        return s.exec(select(func.count()).select_from(ReviewItem).join(Job, Job.id == ReviewItem.job_id).where(
            ReviewItem.kind == "manual", Job.source == source,
            ReviewItem.created_at >= local_day_start_utc())).one()


class Gate:
    def __init__(self, source: str):
        self.source = source

    @property
    def cfg(self) -> SourcePacing:
        cfg = settings().pacing.sources.get(self.source, SourcePacing(enabled=False))
        from .health import pacing_scale
        scale = pacing_scale(self.source)
        if scale > 1.0:  # the error monitor slowed this source down after repeated challenges
            cfg = cfg.model_copy(update={"min_gap_seconds": int(cfg.min_gap_seconds * scale),
                                         "max_gap_seconds": int(cfg.max_gap_seconds * scale)})
        return cfg

    def check(self) -> Decision:
        st = db.source_state(self.source)
        if st.paused and not st.paused_until:
            return Decision(False, st.pause_reason or "paused by you")
        done = prepared_today(self.source) if self.cfg.mode == "assist" else applied_today(self.source)
        return evaluate(self.cfg, local_now(), done, applied_today(),
                        settings().pacing.global_daily_cap, to_local(st.paused_until),
                        to_local(st.next_action_at))

    def record_action(self) -> bool:
        """Call after each apply/browse action. Returns True if a break started."""
        with db.session() as s:
            st = s.get(SourceState, self.source) or SourceState(source=self.source)
            nxt, sess_end, took_break = after_action(self.cfg, local_now(), to_local(st.session_ends_at))
            st.last_action_at = utcnow()
            st.next_action_at = to_utc(nxt)
            st.session_ends_at = to_utc(sess_end)
            s.add(st)
            s.commit()
        if took_break:
            db.log(f"Taking a break until {nxt:%H:%M}", source=self.source, kind="pacing")
        return took_break

    def cooldown(self, reason: str, hours: float | None = None) -> datetime:
        hours = hours if hours is not None else self.cfg.challenge_cooldown_hours
        until = utcnow() + timedelta(hours=hours)
        with db.session() as s:
            st = s.get(SourceState, self.source) or SourceState(source=self.source)
            st.paused, st.paused_until, st.pause_reason = True, until, reason
            st.challenge_count += 1
            s.add(st)
            s.commit()
        return until

    @staticmethod
    def set_paused(source: str, paused: bool, reason: str = "") -> None:
        with db.session() as s:
            st = s.get(SourceState, source) or SourceState(source=source)
            st.paused = paused
            st.paused_until = None
            st.pause_reason = reason if paused else ""
            s.add(st)
            s.commit()

    def clear_expired(self) -> None:
        with db.session() as s:
            st = s.get(SourceState, self.source)
            if st and st.paused_until and st.paused_until <= utcnow():
                st.paused, st.paused_until, st.pause_reason = False, None, ""
                s.add(st)
                s.commit()
                db.log("Cooldown finished, resuming", source=self.source, kind="pacing")
