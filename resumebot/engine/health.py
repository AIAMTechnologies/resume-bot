"""Error monitor: groups recurring problems from the error log, applies known remedies, and tells
you about the rest.

State the remedies set (skipped boards, companies routed to you, slower pacing) lives in the KV
table with an expiry, so every adjustment undoes itself after a while and nothing needs resetting
by hand. `scan()` is pure bookkeeping over the Diagnostic table and is safe to call often.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from .. import db
from ..models import Diagnostic, ErrorPattern, Job, utcnow
from ..profile.master import norm

# ---------------------------------------------------------------- remedy state (KV with expiry)


def _now() -> datetime:
    return utcnow()


def _as_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _live(key: str) -> dict:
    """Entries of a KV dict whose 'until' hasn't passed; expired ones are pruned on read."""
    table = db.kv_get(key) or {}
    live = {k: v for k, v in table.items() if (_as_utc((v or {}).get("until")) or _now()) > _now()}
    if len(live) != len(table):
        db.kv_set(key, live)
    return live


def _set(key: str, entry_key: str, days: float, **fields) -> None:
    table = _live(key)
    table[entry_key] = {"until": (_now() + timedelta(days=days)).isoformat(), **fields}
    db.kv_set(key, table)


def board_skipped(source: str, slug: str) -> bool:
    """True while a dead board slug is being skipped by discovery."""
    return f"{source}:{slug}" in _live("skipped_boards")


def skip_board(source: str, slug: str, days: float = 7, reason: str = "") -> None:
    _set("skipped_boards", f"{source}:{slug}", days, reason=reason)


def company_manual(company: str) -> str:
    """Why this company's applications are prepared for you instead of submitted ('' = not routed)."""
    entry = _live("manual_companies").get(norm(company))
    return (entry or {}).get("reason", "")


def route_company_manual(company: str, reason: str, days: float = 30) -> None:
    _set("manual_companies", norm(company), days, reason=reason)


def pacing_scale(source: str) -> float:
    """Multiplier for a source's gaps between actions (1.0 = configured pacing)."""
    entry = _live("pacing_scale").get(source)
    return float((entry or {}).get("factor", 1.0))


def slow_down(source: str, factor: float = 1.5, days: float = 7, cap: float = 3.0) -> float:
    new = min(cap, round(pacing_scale(source) * factor, 2))
    _set("pacing_scale", source, days, factor=new)
    return new


# ---------------------------------------------------------------- fingerprints & remedies

_FP_RULES = [
    (re.compile(r"https?://\S+"), "URL"),
    (re.compile(r"#\d+"), "#"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "ID"),
    (re.compile(r"\d+(\.\d+)?"), "N"),
    (re.compile(r"[\"“”'‘’].*?[\"“”'‘’]"), "'…'"),
    (re.compile(r"\s+"), " "),
]


def fingerprint(message: str) -> str:
    text = message.strip().lower()
    for rx, repl in _FP_RULES:
        text = rx.sub(repl, text)
    return text[:160]


class Remedy:
    """A known problem shape. `entity` picks the per-thing key (board slug, company, source)."""

    def __init__(self, key: str, pattern: str, threshold: int, status: str, advice: str, kinds: tuple = (),
                 entity=None, action=None, window_days: float = 7):
        self.key, self.rx, self.threshold = key, re.compile(pattern, re.I), threshold
        self.status, self.advice, self.kinds = status, advice, kinds
        self.entity, self.action, self.window_days = entity, action, window_days

    def matches(self, row: Diagnostic) -> bool:
        return (not self.kinds or row.kind in self.kinds) and bool(self.rx.search(row.message))


def _company_of(row: Diagnostic) -> str:
    m = re.search(r"@ (.+?) — ", row.message)
    if m:
        return m.group(1).strip()
    if row.job_id:
        with db.session() as s:
            job = s.get(Job, row.job_id)
        return job.company if job else ""
    return ""


def _source_of(row: Diagnostic) -> str:
    # In "⚠️ <kind> — pausing <source> until …" the source comes after the kind, so try the
    # specific shapes in order rather than taking the leftmost match.
    for rx in (r"pausing (\w+) until", r"^(\w+) session is logged out", r"⚠️ (\w+)"):
        m = re.search(rx, row.message)
        if m:
            return m.group(1)
    return ""


def _board_of(row: Diagnostic) -> str:
    """'<source>:<slug>' from "<source> board '<slug>' …" (discovery logs name the source first)."""
    m = re.search(r"(?:(\w+) )?board '([^']+)'", row.message, re.I)
    return f"{(m.group(1) or '').lower()}:{m.group(2)}" if m else ""


def _act_skip_board(pattern: ErrorPattern, rows: list[Diagnostic]) -> str:
    source, slug = pattern.key.split(":", 2)[1:]
    if not source:
        raise ValueError("the log line does not name the source")
    skip_board(source, slug, days=7, reason=pattern.example[:120])
    return f"Skipping board '{slug}' on {source} for 7 days (fix or remove it in config/companies.yaml)."


def _act_route_manual(pattern: ErrorPattern, rows: list[Diagnostic]) -> str:
    company = pattern.key.split(":", 1)[1]
    route_company_manual(company, f"{pattern.count} automatic attempts failed here; prepared for you to submit")
    return (f"Future {company} applications are prepared for you to submit (30 days) instead of being retried "
            "automatically — the form keeps failing in the same way.")


def _act_slow_down(pattern: ErrorPattern, rows: list[Diagnostic]) -> str:
    source = pattern.key.split(":", 1)[1]
    factor = slow_down(source)
    return (f"{source} is now pacing {factor:.1f}× slower between actions for 7 days after repeated challenges; "
            "the configured cooldown still applies.")


REMEDIES: list[Remedy] = [
    Remedy("board_dead", r"board '(?P<slug>[^']+)' returned (404|410)", 2, "auto",
           "The board slug no longer exists. Fix or remove it in config/companies.yaml.", kinds=("discover",),
           entity=_board_of, action=_act_skip_board),
    Remedy("board_error", r"board '(?P<slug>[^']+)' (failed|returned \d+)", 4, "auto",
           "This board keeps failing. Check the slug and whether the company still uses this ATS.", kinds=("discover",),
           entity=_board_of, action=_act_skip_board),
    Remedy("company_form", r"^(not submitted|apply error)", 2, "auto",
           "Open one of the failed attempts, look at the screenshot, and apply by hand if the form needs "
           "something the bot can't do.", kinds=("apply",), entity=_company_of, action=_act_route_manual,
           window_days=30),
    Remedy("challenge", r"⚠️", 2, "auto",
           "Repeated bot checks on this source. Keep daily caps modest; if it keeps happening, pause the "
           "source for a few days.", kinds=("challenge",), entity=_source_of, action=_act_slow_down),
    Remedy("ai_json", r"not valid json|no json in model reply|bad json in model reply", 3, "watching",
           "The bot already re-asks once per call. If this keeps growing, try a different LLM_MODEL.", kinds=("llm", "score", "tailor")),
    Remedy("ai_limit", r"out of allowance|quota unavailable|allowance exhausted", 1, "watching",
           "AI plan allowance ran out; work resumes automatically when it resets. Set LLM_FALLBACK=codex_cli "
           "or LLM_BACKEND=anthropic_api to keep going."),
    Remedy("cli_missing", r"claude code cli not found", 1, "needs-you",
           "Install the Claude Code CLI or set CLAUDE_CLI_PATH in .env (or LLM_BACKEND=anthropic_api)."),
    Remedy("logged_out", r"session is logged out", 1, "needs-you",
           "Run `resumebot login <source>` on the Mac, then resume the source."),
    Remedy("gmail", r"gmail connection expired|gmail (api|imap)", 1, "needs-you",
           "Run `resumebot gmail-auth` again (or check the app password in .env)."),
    Remedy("telegram", r"telegram (send|document send|poll) (failed|error)", 5, "needs-you",
           "Check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID and the Mac's network; the bot keeps retrying."),
    Remedy("optional_field", r"skipped optional field", 5, "watching",
           "Optional fields that never responded were left blank; applications still went through."),
    Remedy("scoring", r"^scoring failed", 3, "watching",
           "Jobs that fail scoring three times are parked in Review with the error; use Score now to retry."),
]
UNKNOWN_ALERT_AT = 3   # an error shape nobody wrote a remedy for: tell you once it has recurred this often


def classify(row: Diagnostic) -> tuple[Remedy | None, str]:
    for remedy in REMEDIES:
        if remedy.matches(row):
            entity = ""
            if remedy.entity:
                try:
                    entity = remedy.entity(row) or ""
                except Exception:  # noqa: BLE001
                    entity = ""
            return remedy, f"{remedy.key}:{entity}" if remedy.entity else remedy.key
    return None, f"unknown:{fingerprint(row.message)}"


# ---------------------------------------------------------------- scanning


def _new_rows(limit: int = 1000) -> list[Diagnostic]:
    since = _as_utc(db.kv_get("health_last_scan"))
    with db.session() as s:
        query = select(Diagnostic).where(Diagnostic.level.in_(["error", "warning"]))
        if since:
            query = query.where(Diagnostic.ts > since)
        return list(s.exec(query.order_by(Diagnostic.ts).limit(limit)))


def scan(notify: bool = True) -> list[ErrorPattern]:
    """Fold new error-log entries into patterns, run remedies that crossed their threshold, and
    return every pattern that changed. Cheap when nothing is new."""
    rows = _new_rows()
    if not rows:
        return []
    touched: dict[str, tuple[ErrorPattern, list[Diagnostic]]] = {}
    with db.session() as s:
        for row in rows:
            remedy, key = classify(row)
            pattern = s.exec(select(ErrorPattern).where(ErrorPattern.key == key)).first()
            if pattern is None:
                pattern = ErrorPattern(key=key, fingerprint=fingerprint(row.message), kind=row.kind,
                                       first_seen=row.ts, status=remedy.status if remedy and remedy.threshold <= 1
                                       else "watching", advice=remedy.advice if remedy else "")
            elif pattern.status == "resolved" or (remedy and pattern.last_seen and
                                                   pattern.last_seen < row.ts - timedelta(days=remedy.window_days)):
                pattern.count, pattern.action, pattern.alerted, pattern.status = 0, "", False, "watching"
            pattern.count += 1
            pattern.last_seen, pattern.example, pattern.kind = row.ts, row.message[:500], row.kind
            if row.job_id and row.job_id not in (pattern.job_ids or []):
                pattern.job_ids = [*(pattern.job_ids or [])[-4:], row.job_id]
            s.add(pattern)
            s.commit()
            s.refresh(pattern)
            touched.setdefault(key, (pattern, []))[1].append(row)
        db.kv_set("health_last_scan", rows[-1].ts.isoformat())

        changed = []
        for key, (pattern, group) in touched.items():
            remedy = next((r for r in REMEDIES if key == r.key or key.startswith(r.key + ":")), None)
            note = ""
            if remedy:
                if pattern.count >= remedy.threshold and not pattern.action:
                    pattern.status = remedy.status
                    if remedy.action:
                        try:
                            pattern.action = remedy.action(pattern, group)
                            note = pattern.action
                        except Exception as error:  # noqa: BLE001
                            pattern.action = f"remedy failed: {error}"[:300]
                    else:
                        pattern.action = "—"
                        note = pattern.advice if pattern.status == "needs-you" else ""
            elif pattern.count >= UNKNOWN_ALERT_AT and not pattern.alerted:
                pattern.status, pattern.advice = "needs-you", "No automatic remedy for this yet. Open the error log for the traceback."
                note = f"Recurring problem (×{pattern.count}) with no automatic fix yet: {pattern.example[:160]}"
            if note and notify and not pattern.alerted:
                pattern.alerted = True
                db.log(f"🩺 {note}", kind="health", level="warning" if pattern.status == "needs-you" else "info")
                _notify(note, pattern.status)
            s.add(pattern)
            s.commit()
            s.refresh(pattern)
            changed.append(pattern)
    return changed


def _notify(note: str, status: str) -> None:
    from ..notify import telegram
    head = "🩺 <b>Needs you</b>" if status == "needs-you" else "🩺 <b>Auto-adjusted</b>"
    telegram.notify(f"{head}\n{telegram.esc(note)}\nDetails: Error log → Recurring problems on the dashboard.")


def patterns(limit: int = 50, include_resolved: bool = False) -> list[ErrorPattern]:
    with db.session() as s:
        query = select(ErrorPattern)
        if not include_resolved:
            query = query.where(ErrorPattern.status != "resolved")
        return list(s.exec(query.order_by(ErrorPattern.last_seen.desc()).limit(limit)))


def needs_you() -> list[ErrorPattern]:
    return [p for p in patterns() if p.status == "needs-you"]


def resolve(pattern_id: int) -> str:
    with db.session() as s:
        pattern = s.get(ErrorPattern, pattern_id)
        if not pattern:
            return "Unknown pattern."
        pattern.status, pattern.alerted = "resolved", False
        s.add(pattern)
        s.commit()
        return f"Marked resolved: {pattern.example[:80]}"


def summary_line() -> str:
    """One line for the daily Telegram summary ('' when all is quiet)."""
    open_ = patterns()
    yours = [p for p in open_ if p.status == "needs-you"]
    auto = [p for p in open_ if p.status == "auto"]
    if not yours and not auto:
        return ""
    bits = []
    if yours:
        bits.append(f"{len(yours)} problem(s) need you")
    if auto:
        bits.append(f"{len(auto)} auto-adjustment(s) active")
    return "🩺 " + " · ".join(bits) + " — Error log on the dashboard"
