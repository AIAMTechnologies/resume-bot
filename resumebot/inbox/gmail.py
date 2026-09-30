"""Watch the dedicated application Gmail (IMAP + app password) and classify replies."""
from __future__ import annotations

import asyncio
import email
import imaplib
import re
from email.header import decode_header, make_header
from email.utils import parseaddr

from sqlmodel import select

from .. import db
from ..config import env
from ..llm import complete_json
from ..models import Application, AppStatus, utcnow
from ..notify import telegram
from ..notify.telegram import esc
from ..profile.master import norm

RANK = {AppStatus.SUBMITTED: 0, AppStatus.FAILED: 0, AppStatus.CONFIRMED: 1, AppStatus.GHOSTED: 1,
        AppStatus.REJECTED: 2, AppStatus.ASSESSMENT: 3, AppStatus.INTERVIEW: 4, AppStatus.OFFER: 5}

RULES = [
    (AppStatus.OFFER, re.compile(r"pleased to (extend|offer)|offer letter|job offer", re.I)),
    (AppStatus.REJECTED, re.compile(r"unfortunately|not (be )?moving forward|decided to (move|proceed|pursue) "
                                    r"(forward )?with other|not selected|position has been filled|regret to inform", re.I)),
    (AppStatus.ASSESSMENT, re.compile(r"assessment|coding (challenge|exercise)|take-?home|hackerrank|codesignal|"
                                      r"codility|testgorilla", re.I)),
    (AppStatus.INTERVIEW, re.compile(r"schedule (a|an|your) (call|interview|chat)|interview|calendly\.com|"
                                     r"availability for|next steps? .{0,40}(call|chat)", re.I)),
    (AppStatus.CONFIRMED, re.compile(r"thank(s| you) for (applying|your application|your interest)|"
                                     r"received your application|application (was )?received", re.I)),
]


def _decode(s: str | None) -> str:
    return str(make_header(decode_header(s))) if s else ""


def _body(msg: email.message.Message) -> str:
    parts = msg.walk() if msg.is_multipart() else [msg]
    text, html_text = "", ""
    for p in parts:
        ctype = p.get_content_type()
        if p.get_content_disposition() == "attachment":
            continue
        try:
            payload = p.get_payload(decode=True).decode(p.get_content_charset() or "utf-8", errors="ignore")
        except Exception:
            continue
        if ctype == "text/plain":
            text += payload
        elif ctype == "text/html":
            html_text += payload
    if not text and html_text:
        from bs4 import BeautifulSoup
        text = BeautifulSoup(html_text, "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", text)[:6000]


def fetch_new(last_uid: int) -> list[tuple[int, str, str, str]]:
    """Returns [(uid, from, subject, body)] for messages after last_uid."""
    m = imaplib.IMAP4_SSL("imap.gmail.com")
    m.login(env().gmail_address, env().gmail_app_password)
    m.select("INBOX", readonly=True)
    typ, data = m.uid("search", None, f"UID {last_uid + 1}:*")
    out = []
    for uid in (data[0].split() if data and data[0] else []):
        uid_i = int(uid)
        if uid_i <= last_uid:
            continue
        typ, msg_data = m.uid("fetch", uid, "(RFC822)")
        if not msg_data or not msg_data[0]:
            continue
        msg = email.message_from_bytes(msg_data[0][1])
        out.append((uid_i, _decode(msg.get("From")), _decode(msg.get("Subject")), _body(msg)))
    m.logout()
    return out


def match_application(sender: str, subject: str, body: str) -> Application | None:
    name, addr = parseaddr(sender)
    domain = addr.split("@")[-1].split(".")[-2] if "@" in addr else ""
    hay = norm(f"{name} {domain} {subject} {body[:1500]}")
    with db.session() as s:
        apps = list(s.exec(select(Application).where(Application.submitted_at.is_not(None))
                           .order_by(Application.submitted_at.desc()).limit(500)))
    best = None
    for a in apps:
        company = norm(a.company)
        if company and (f" {company} " in f" {hay} " or (domain and norm(domain) == company.replace(" ", ""))):
            if best is None:
                best = a
            if norm(a.title) and norm(a.title) in hay:
                return a
    return best


async def classify(subject: str, body: str) -> tuple[str, str]:
    text = f"{subject}\n{body}"
    hits = [status for status, rx in RULES if rx.search(text)]
    if len(hits) == 1:
        return hits[0], ""
    if AppStatus.REJECTED in hits and AppStatus.OFFER not in hits:
        return AppStatus.REJECTED, ""  # rejections often thank you for applying, too
    # Ambiguous (e.g. "thanks for applying — we'll reach out to schedule an interview if…"): ask the model.
    result = await complete_json(
        f"Classify this recruiting email.\nSUBJECT: {subject}\nBODY: {body[:3000]}\n"
        'Return {"status": "confirmed|rejected|assessment|interview|offer|other", "summary": "one line"}')
    status = result.get("status", "other")
    return (status if status in RANK else "other"), result.get("summary", "")


async def _process(sender: str, subject: str, body: str) -> None:
    app = match_application(sender, subject, body)
    if not app:
        return
    status, summary = await classify(subject, body)
    if status == "other" or RANK.get(status, 0) < RANK.get(app.status, 0):
        return
    app.status = status
    app.last_response_at = utcnow()
    app.response_summary = summary or subject[:200]
    db.save(app)
    db.log(f"📬 {app.company}: {status} — {subject[:80]}", kind="inbox",
           level="success" if status in (AppStatus.INTERVIEW, AppStatus.OFFER, AppStatus.ASSESSMENT) else "info",
           job_id=app.job_id)
    if status in (AppStatus.INTERVIEW, AppStatus.ASSESSMENT, AppStatus.OFFER):
        await telegram.send(f"🎉 <b>{esc(status.title())}</b> — {esc(app.title)} at {esc(app.company)}\n"
                            f"<i>{esc(subject)}</i>")


async def check_inbox() -> None:
    from . import gmail_api
    if gmail_api.configured():
        try:
            await _check_api(gmail_api)
            db.kv_set("gmail_reauth_notified", False)
        except gmail_api.NeedsReauth:
            if not db.kv_get("gmail_reauth_notified"):
                db.log("Gmail connection expired — run: resumebot gmail-auth", level="warning", kind="inbox")
                await telegram.send("🔑 <b>Gmail needs reconnecting</b> (Google does this weekly for private apps).\n"
                                    "On the Mac run:\n<code>cd ~/Documents/Resume\\ Bot && .venv/bin/resumebot gmail-auth</code>")
                db.kv_set("gmail_reauth_notified", True)
        return
    await _check_imap()


async def _check_api(gmail_api) -> None:
    last_ts = int(db.kv_get("gmail_last_ts", 0))
    if last_ts == 0:
        import time
        db.kv_set("gmail_last_ts", int(time.time() * 1000))  # start from now
        db.log("Inbox tracker connected (Gmail API)", kind="inbox", level="success")
        return
    for ts, sender, subject, body in await asyncio.to_thread(gmail_api.fetch_since, last_ts):
        await _process(sender, subject, body)
        db.kv_set("gmail_last_ts", ts)


async def _check_imap() -> None:
    last_uid = int(db.kv_get("gmail_last_uid", 0))
    if last_uid == 0:
        # First run: start from now, don't re-process the whole mailbox.
        msgs = await asyncio.to_thread(fetch_new, 0)
        db.kv_set("gmail_last_uid", max((u for u, *_ in msgs), default=0))
        db.log("Inbox tracker connected", kind="inbox", level="success")
        return
    msgs = await asyncio.to_thread(fetch_new, last_uid)
    for uid, sender, subject, body in msgs:
        await _process(sender, subject, body)
        db.kv_set("gmail_last_uid", uid)
