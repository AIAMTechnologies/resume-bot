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
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1].lower() if len(parts) > 1 else "all"
    targets = list(SOURCES) if arg == "all" else [arg]
    if cmd in ("/start", "/help"):
        return HELP
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
        db.kv_set("run_now", True)
        return "OK — skipping the current wait on the next scheduler tick."
    if cmd == "/dashboard":
        return f"http://{env().dashboard_host}:{env().dashboard_port}"
    return "Unknown command. /help"


async def handle_callback(data: str) -> str:
    from ..engine import review
    kind, action, item_id = data.split(":")
    if kind != "rv":
        return "?"
    return review.resolve(int(item_id), action)


async def poll_forever() -> None:
    """Long-poll Telegram for commands, button presses, and replies."""
    if not enabled():
        return
    from ..engine import review
    offset = db.kv_get("telegram_offset", 0)
    chat_id = str(env().telegram_chat_id)
    await call("setMyCommands", commands=[
        {"command": c, "description": d} for c, d in [
            ("status", "Sources and today's numbers"), ("today", "Today's applications"),
            ("queue", "Pending review items"), ("pause", "Pause a source or all"),
            ("resume", "Resume a source or all"), ("run", "Apply now"), ("dashboard", "Dashboard link")]])
    while True:
        try:
            updates = await call("getUpdates", offset=offset, timeout=50,
                                 allowed_updates=["message", "callback_query"])
        except Exception as e:  # noqa: BLE001
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
                    if m.get("reply_to_message"):
                        reply = review.answer_by_telegram_message(m["reply_to_message"]["message_id"], text)
                        await send(esc(reply), reply_to=m["message_id"])
                    elif text.startswith("/"):
                        await send(await handle_command(text))
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
