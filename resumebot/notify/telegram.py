"""Telegram bot: push notifications, inline approve/skip buttons, and control commands.

Only messages from TELEGRAM_CHAT_ID are obeyed; everyone else is ignored.
"""
from __future__ import annotations

import asyncio
import html
from typing import Any

import httpx

from .. import db
from ..config import env

API = "https://api.telegram.org/bot{token}/{method}"


def enabled() -> bool:
    return bool(env().telegram_bot_token and env().telegram_chat_id)


async def call(method: str, **params: Any) -> Any:
    async with httpx.AsyncClient(timeout=70) as c:
        r = await c.post(API.format(token=env().telegram_bot_token, method=method), json=params)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
        return data["result"]


def esc(s: Any) -> str:
    return html.escape(str(s or ""))


async def send(text: str, buttons: list[list[tuple[str, str]]] | None = None, reply_to: int | None = None
               ) -> int | None:
    """Send an HTML message. buttons: rows of (label, callback_data | url)."""
    if not enabled():
        return None
    params: dict[str, Any] = {"chat_id": env().telegram_chat_id, "text": text[:4000], "parse_mode": "HTML",
                              "disable_web_page_preview": True}
    if buttons:
        params["reply_markup"] = {"inline_keyboard": [
            [{"text": label, **({"url": data} if data.startswith("http") else {"callback_data": data})}
             for label, data in row] for row in buttons]}
    if reply_to:
        params["reply_parameters"] = {"message_id": reply_to}
    try:
        msg = await call("sendMessage", **params)
        return msg["message_id"]
    except Exception as e:  # noqa: BLE001
        db.log(f"Telegram send failed: {e}", level="warning", kind="notify")
        return None


async def send_document(path: str, caption: str = "") -> int | None:
    """Send a file (e.g. the tailored resume PDF) so you can apply straight from your phone."""
    if not enabled():
        return None
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            with open(path, "rb") as f:
                r = await c.post(API.format(token=env().telegram_bot_token, method="sendDocument"),
                                 data={"chat_id": env().telegram_chat_id, "caption": caption[:1000],
                                       "parse_mode": "HTML"},
                                 files={"document": (path.rsplit("/", 1)[-1], f, "application/pdf")})
        data = r.json()
        return data["result"]["message_id"] if data.get("ok") else None
    except Exception as e:  # noqa: BLE001
        db.log(f"Telegram document send failed: {e}", level="warning", kind="notify")
        return None


def notify(text: str, buttons: list[list[tuple[str, str]]] | None = None) -> None:
    """Fire-and-forget from anywhere inside the event loop."""
    if not enabled():
        return
    try:
        asyncio.get_running_loop().create_task(send(text, buttons))
    except RuntimeError:
        asyncio.run(send(text, buttons))


# ---------------- command handling ----------------

HELP = """<b>Resume Bot</b>
/status – sources, today's counts, next actions
/today – applications submitted today
/queue – pending review items
/jobs – jobs available to apply to
/preview JOB_ID – generate and send the exact resume
/apply JOB_ID – preview and confirm an application
/pause [source|all] – pause (default all)
/resume [source|all] – resume
/run – start applying now (ignores the current wait, not caps/hours)
/dashboard – dashboard link
Reply to a question message to answer it."""


async def _status_text() -> str:
    from ..engine import stats
    s = stats.overview()
    lines = [f"<b>Today</b>: {s['today']} applied · <b>Week</b>: {s['week']} · <b>Total</b>: {s['total']}",
             f"Review queue: {s['pending_reviews']} · Queued jobs: {s['queued']}",
             f"Responses: {s['responses']} · Interviews: {s['interviews']}", ""]
    for src in stats.source_health():
        state = "⏸ " + esc(src["reason"]) if src["paused"] else ("▶️ " + esc(src["status"]))
        lines.append(f"<b>{src['name']}</b> {src['today']}/{src['cap']} — {state}")
    return "\n".join(lines)


async def handle_command(text: str) -> str:
    from ..engine import stats
    from ..engine.pacing import Gate
    from ..sources import SOURCES

    parts = text.strip().split()
    if not parts:
        return HELP
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1].lower() if len(parts) > 1 else "all"
    targets = list(SOURCES) if arg == "all" else [arg]
    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/jobs":
        from ..models import Job, JobStatus
        with db.session() as s:
            jobs = s.exec(db.select(Job).where(Job.status.notin_([JobStatus.APPLIED, JobStatus.SKIPPED]))
                          .order_by(Job.match_score.desc()).limit(15)).all()
        return ("\n".join(f"#{j.id} · {esc(j.title)} — {esc(j.company)} ({j.status})" for j in jobs)
                + "\n\nUse /preview JOB_ID or /apply JOB_ID.") if jobs else "No available jobs."
    if cmd in ("/preview", "/apply"):
        from ..engine import actions, drafts
        if not arg.isdigit():
            return f"Usage: {cmd} JOB_ID (use /jobs to find IDs)."
        job_id = int(arg)
        try:
            if (drafts.get(job_id) or {}).get("status") == "ready":
                await send_preview(job_id)
                return "Review the PDF and use its confirmation button."
            return actions.start(job_id, "preview", notify=True)
        except ValueError as error:
            return str(error)
    if cmd == "/status":
        return await _status_text()
    if cmd == "/today":
        apps = stats.recent_applications(today_only=True)
        if not apps:
            return "No applications yet today."
        return "\n".join(f"• {esc(a.title)} — {esc(a.company)} <i>({a.source})</i>" for a in apps)
    if cmd == "/queue":
        items = stats.pending_reviews()
        if not items:
            return "Review queue is empty. 🎉"
        return "\n".join(f"#{i.id} [{i.kind}] {esc(i.question or i.context)[:120]}" for i in items[:20])
    if cmd in ("/pause", "/resume"):
        for t in targets:
            if t not in SOURCES:
                return f"Unknown source '{esc(t)}'. Sources: {', '.join(SOURCES)}"
            Gate.set_paused(t, cmd == "/pause", "paused via Telegram")
        db.log(f"{cmd[1:].title()}d {arg} via Telegram", kind="control")
        return f"{'Paused' if cmd == '/pause' else 'Resumed'}: {arg}"
    if cmd == "/run":
        if not db.kv_get("automatic_mode", False):
            return "Manual mode: use /jobs, then /apply JOB_ID to preview and confirm."
        db.kv_set("run_now", True)
        return "OK — skipping the current wait on the next scheduler tick."
    if cmd == "/dashboard":
        return f"http://{env().dashboard_host}:{env().dashboard_port}"
    return "Unknown command. /help"


async def handle_callback(data: str) -> str:
    from ..engine import review
    if data.startswith("apply:"):
        from ..engine import actions
        try:
            _, job_id, version = data.split(":")
            return actions.start(int(job_id), "apply", version, notify=True)
        except (ValueError, TypeError) as error:
            return str(error)
    if data.startswith("preview:"):
        from ..engine import actions
        try:
            return actions.start(int(data.split(":")[1]), "preview", notify=True)
        except (ValueError, TypeError) as error:
            return str(error)
    if len(data.split(":")) != 3:
        return "Unknown button. Use /help."
    kind, action, item_id = data.split(":")
    if kind != "rv" or not item_id.isdigit():
        return "?"
    # Existing messages still have legacy Apply buttons. Make them preview-first too.
    if action == "approve":
        from ..engine import actions
        from ..models import ReviewItem
        with db.session() as s:
            item = s.get(ReviewItem, int(item_id))
        if not item or item.status != "pending":
            return "Already handled. Use /jobs to preview the job."
        try:
            return actions.start(item.job_id, "preview", notify=True)
        except ValueError as error:
            return str(error)
    return review.resolve(int(item_id), action)


async def poll_forever() -> None:
    """Long-poll Telegram for commands, button presses, and replies."""
    db.kv_set("telegram_connected", False)
    if not enabled():
        return
    from ..engine import review
    offset = db.kv_get("telegram_offset", 0)
    chat_id = str(env().telegram_chat_id)
    commands = [
        {"command": c, "description": d} for c, d in [
            ("status", "Sources and today's numbers"), ("today", "Today's applications"),
            ("queue", "Pending review items"), ("pause", "Pause a source or all"),
            ("resume", "Resume a source or all"), ("run", "Run scheduler"), ("dashboard", "Dashboard link"),
            ("jobs", "List jobs and IDs"), ("preview", "Preview resume: /preview JOB_ID"),
            ("apply", "Preview and confirm: /apply JOB_ID")]]
    registered = False
    while True:
        try:
            if not registered:
                await call("setMyCommands", commands=commands)
                registered = True
                db.kv_set("telegram_connected", True)
            updates = await call("getUpdates", offset=offset, timeout=50,
                                 allowed_updates=["message", "callback_query"])
            db.kv_set("telegram_connected", True)
        except Exception as e:  # noqa: BLE001
            db.kv_set("telegram_connected", False)
            db.log(f"Telegram poll error: {e}", level="warning", kind="notify")
            await asyncio.sleep(15)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            db.kv_set("telegram_offset", offset)
            try:
                if "callback_query" in u:
                    cq = u["callback_query"]
                    if str(cq["message"]["chat"]["id"]) != chat_id:
                        continue
                    result = await handle_callback(cq.get("data", ""))
                    await call("answerCallbackQuery", callback_query_id=cq["id"], text=result[:200])
                    await call("editMessageReplyMarkup", chat_id=chat_id,
                               message_id=cq["message"]["message_id"], reply_markup={"inline_keyboard": []})
                    await send(f"✅ {esc(result)}", reply_to=cq["message"]["message_id"])
                elif "message" in u:
                    m = u["message"]
                    if str(m["chat"]["id"]) != chat_id:
                        continue
                    text = m.get("text", "")
                    if text.startswith("/"):
                        await send(await handle_command(text))
                    elif m.get("reply_to_message"):
                        reply = review.answer_by_telegram_message(m["reply_to_message"]["message_id"], text)
                        await send(esc(reply), reply_to=m["message_id"])
            except Exception as e:  # noqa: BLE001
                db.log(f"Telegram handler error: {e}", level="error", kind="notify")


async def discover_chat_ids() -> list[tuple[str, str]]:
    updates = await call("getUpdates", timeout=0)
    seen = {}
    for u in updates:
        chat = (u.get("message") or {}).get("chat")
        if chat:
            seen[str(chat["id"])] = chat.get("username") or chat.get("first_name", "")
    return list(seen.items())


async def send_preview(job_id: int) -> None:
    from ..engine import drafts
    from ..models import Job, JobStatus
    with db.session() as s:
        job = s.get(Job, job_id)
    if not job:
        raise ValueError('Job not found.')
    materials, _ = drafts.load(job_id)
    draft = drafts.get(job_id)
    message_id = await send_document(str(materials.resume_pdf),
        f'Resume preview #{job_id} — {esc(job.title)} @ {esc(job.company)}')
    if message_id is None:
        raise ValueError('Could not deliver the preview PDF. Retry /preview or use the dashboard.')
    manual = job.source in ('linkedin', 'workday', 'external')
    buttons = [] if job.status in (JobStatus.APPLIED, JobStatus.APPLYING) else [[(
        'Prepare manual application' if manual else 'Apply now with this resume',
        f'apply:{job_id}:{draft["version"]}')]]
    await send(f'<b>Preview #{job_id}</b> — {esc(job.title)} @ {esc(job.company)}\n'
               'The saved resume above will be used unchanged.\n' +
               ('You finish submission on the job website.' if manual else
                'The button submits this job immediately, outside the automatic schedule.'), buttons)
