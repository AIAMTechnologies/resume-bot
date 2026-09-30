"""Read-side queries for the dashboard and Telegram."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta

from sqlmodel import func, select

from .. import db
from ..config import settings
from ..models import Application, AppStatus, Event, Job, JobStatus, ReviewItem, SourceState, utcnow
from .pacing import Gate, applied_today, local_day_start_utc, local_now, prepared_today, to_local

RESPONSE_STATUSES = [AppStatus.REJECTED, AppStatus.ASSESSMENT, AppStatus.INTERVIEW, AppStatus.OFFER]
POSITIVE = [AppStatus.ASSESSMENT, AppStatus.INTERVIEW, AppStatus.OFFER]


def _count(s, model, *where) -> int:
    return s.exec(select(func.count()).select_from(model).where(*where)).one()


def overview() -> dict:
    today0 = local_day_start_utc()
    with db.session() as s:
        submitted = Application.submitted_at.is_not(None)
        total = _count(s, Application, submitted)
        responses = _count(s, Application, Application.status.in_(RESPONSE_STATUSES))
        interviews = _count(s, Application, Application.status.in_([AppStatus.INTERVIEW, AppStatus.OFFER]))
        avg_ats = s.exec(select(func.avg(Application.ats_score)).where(Application.ats_score.is_not(None))).one()
        avg_match = s.exec(select(func.avg(Job.match_score)).join(Application, Application.job_id == Job.id)).one()
        return {
            "total": total,
            "today": _count(s, Application, Application.submitted_at >= today0),
            "week": _count(s, Application, Application.submitted_at >= today0 - timedelta(days=6)),
            "discovered": _count(s, Job),
            "queued": _count(s, Job, Job.status == JobStatus.QUEUED),
            "manual": _count(s, Job, Job.status == JobStatus.MANUAL),
            "failed": _count(s, Application, Application.status == AppStatus.FAILED),
            "pending_reviews": _count(s, ReviewItem, ReviewItem.status == "pending"),
            "responses": responses,
            "interviews": interviews,
            "response_rate": round(100 * responses / total, 1) if total else 0.0,
            "avg_ats": round(avg_ats or 0),
            "avg_match": round(avg_match or 0),
        }


def source_health() -> list[dict]:
    out = []
    cfgs = settings().pacing.sources
    with db.session() as s:
        states = {st.source: st for st in s.exec(select(SourceState))}
    for name, cfg in cfgs.items():
        st = states.get(name)
        decision = Gate(name).check()
        cap = cfg.weekend_cap if local_now().weekday() >= 5 else cfg.daily_cap
        out.append({
            "name": name,
            "enabled": cfg.enabled,
            "mode": cfg.mode,
            "paused": bool(st and st.paused),
            "reason": (st.pause_reason if st and st.paused else ""),
            "status": "ready" if decision.ok else decision.reason,
            "next": decision.retry_at.strftime("%a %H:%M") if decision.retry_at else "",
            "today": prepared_today(name) if cfg.mode == "assist" else applied_today(name),
            "cap": cap,
            "last_discover": to_local(st.last_discover_at).strftime("%a %H:%M") if st and st.last_discover_at else "never",
            "challenges": st.challenge_count if st else 0,
        })
    return out


def daily_series(days: int = 30) -> dict:
    """{"labels": [...dates], "series": {source: [counts]}} in local days."""
    start = local_now().date() - timedelta(days=days - 1)
    labels = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    counts: dict[str, Counter] = defaultdict(Counter)
    with db.session() as s:
        rows = s.exec(select(Application.source, Application.submitted_at).where(
            Application.submitted_at >= utcnow() - timedelta(days=days + 1))).all()
    for source, ts in rows:
        day = to_local(ts).date().isoformat()
        counts[source][day] += 1
    order = [n for n in settings().pacing.sources if n in counts] + [n for n in counts if n not in settings().pacing.sources]
    return {"labels": labels, "series": {src: [counts[src][d] for d in labels] for src in order}}


def funnel() -> list[dict]:
    with db.session() as s:
        discovered = _count(s, Job)
        passed = _count(s, Job, Job.match_score.is_not(None))
        matched = _count(s, Job, Job.match_score >= settings().matching.review_score)
        applied = _count(s, Application, Application.submitted_at.is_not(None))
        responded = _count(s, Application, Application.status.in_(RESPONSE_STATUSES))
        positive = _count(s, Application, Application.status.in_(POSITIVE))
    return [{"stage": "Discovered", "n": discovered}, {"stage": "Passed filters", "n": passed},
            {"stage": "Good match", "n": matched}, {"stage": "Applied", "n": applied},
            {"stage": "Heard back", "n": responded}, {"stage": "Assessment / interview", "n": positive}]


def by_source() -> list[dict]:
    with db.session() as s:
        rows = s.exec(select(Application.source, Application.status, func.count()).group_by(
            Application.source, Application.status)).all()
    agg: dict[str, Counter] = defaultdict(Counter)
    for src, status, n in rows:
        agg[src][status] += n
    return [{"source": src, "applied": sum(c.values()) - c[AppStatus.FAILED], "failed": c[AppStatus.FAILED],
             "responses": sum(c[x] for x in RESPONSE_STATUSES), "interviews": c[AppStatus.INTERVIEW] + c[AppStatus.OFFER]}
            for src, c in sorted(agg.items())]


def skip_reasons(limit: int = 8) -> list[tuple[str, int]]:
    with db.session() as s:
        rows = s.exec(select(Job.status_reason).where(Job.status == JobStatus.SKIPPED)).all()
    norm = Counter((r or "unknown").split(" (")[0].split(" #")[0][:50] for r in rows)
    return norm.most_common(limit)


def recent_applications(limit: int = 50, today_only: bool = False, source: str = "", status: str = "",
                        q: str = "") -> list[Application]:
    with db.session() as s:
        stmt = select(Application).order_by(Application.started_at.desc())
        if today_only:
            stmt = stmt.where(Application.submitted_at >= local_day_start_utc())
        if source:
            stmt = stmt.where(Application.source == source)
        if status:
            stmt = stmt.where(Application.status == status)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(Application.company.ilike(like) | Application.title.ilike(like))
        return list(s.exec(stmt.limit(limit)))


def jobs(status: str = "", source: str = "", limit: int = 200) -> list[Job]:
    with db.session() as s:
        stmt = select(Job).order_by(Job.discovered_at.desc())
        if status:
            stmt = stmt.where(Job.status == status)
        if source:
            stmt = stmt.where(Job.source == source)
        return list(s.exec(stmt.limit(limit)))


def pending_reviews() -> list[ReviewItem]:
    with db.session() as s:
        return list(s.exec(select(ReviewItem).where(ReviewItem.status == "pending")
                           .order_by(ReviewItem.created_at.desc())))


def recent_events(limit: int = 60) -> list[Event]:
    with db.session() as s:
        return list(s.exec(select(Event).order_by(Event.ts.desc()).limit(limit)))


def automation() -> dict:
    from . import actions, runtime
    cfg = settings()
    now = local_now()
    with db.session() as s:
        counts = dict(s.exec(select(Job.status, func.count()).group_by(Job.status)).all())
        pending = list(s.exec(select(Job).where(Job.status.in_([
            JobStatus.APPLYING, JobStatus.QUEUED, JobStatus.REVIEW, JobStatus.MANUAL]))
            .order_by(Job.match_score.desc()).limit(30)))
    workers = runtime.snapshot()
    workers_by_name = {w["name"]: w for w in workers}
    health = source_health()
    for source in health:
        source["worker"] = workers_by_name.get(f"apply:{source['name']}")
        hours = cfg.pacing.sources[source['name']].active_hours
        source['hours'] = f'{hours[0]:02}:00–{hours[1]:02}:00'
        source['queued'] = sum(1 for j in pending if j.source == source['name'] and j.status == JobStatus.QUEUED)
        opening = now.replace(hour=hours[0], minute=0, second=0, microsecond=0)
        if opening <= now:
            opening += timedelta(days=1)
        source['opens_at'] = opening.strftime('%a %H:%M') if source['status'] == 'outside active hours' else ''
    health_by_name = {h['name']: h for h in health}
    queue = []
    for job in sorted(pending, key=lambda j: (j.status != JobStatus.APPLYING, j.status != JobStatus.QUEUED)):
        source = health_by_name.get(job.source, {})
        if actions.busy(job.id):
            reason = 'Preparing preview / processing your action'
        elif job.status == JobStatus.APPLYING:
            reason = 'Tailoring resume or filling the application'
        elif job.status == JobStatus.MANUAL:
            reason = 'Ready for you to submit on the job website'
        elif job.status == JobStatus.REVIEW:
            reason = job.status_reason or 'Needs your review'
        elif not db.kv_get('automatic_mode'):
            reason = 'Waiting for automated mode'
        elif db.kv_get('global_pause'):
            reason = 'Global pause is on'
        else:
            reason = source.get('reason') or source.get('status', 'Manual source')
            if source.get('opens_at'):
                reason += f" · eligible {source['opens_at']}"
            if job.source == 'linkedin':
                reason += ' · prepares a manual application'
        queue.append({'job': job, 'reason': reason})
    return {'enabled': bool(db.kv_get('automatic_mode')), 'global_pause': bool(db.kv_get('global_pause')),
            'now': now, 'timezone': cfg.timezone, 'counts': counts, 'sources': health,
            'workers': workers, 'live': any(w['name'] == 'triage' and w['phase'] not in ('stopped','failed') for w in workers),
            'queue': queue, 'events': recent_events(8)}
