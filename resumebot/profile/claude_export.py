"""Parse a claude.ai data export (Settings → Privacy → Export data).

The export zip contains conversations.json (and projects.json). We keep conversations that
look like real building work, then hand them to the LLM in batches to identify projects.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

BUILD_HINTS = re.compile(
    r"```|deploy|docker|api|database|schema|react|python|typescript|endpoint|refactor|bug|"
    r"pipeline|dashboard|script|automation|model|kubernetes|aws|supabase|postgres|frontend|backend",
    re.I,
)


def _messages(conv: dict) -> list[dict]:
    return conv.get("chat_messages") or conv.get("messages") or []


def _text(msg: dict) -> str:
    if msg.get("text"):
        return msg["text"]
    return "\n".join(c.get("text", "") for c in msg.get("content", []) if isinstance(c, dict))


def load(export_dir: Path) -> tuple[list[dict], list[dict]]:
    convs, projects = [], []
    for f in export_dir.rglob("conversations.json"):
        convs.extend(json.loads(f.read_text()))
    for f in export_dir.rglob("projects.json"):
        projects.extend(json.loads(f.read_text()))
    return convs, projects


def digests(export_dir: Path, per_conv_chars: int = 3000) -> list[str]:
    """One compact digest per build-like conversation or project."""
    convs, projects = load(export_dir)
    out: list[str] = []
    for p in projects:
        docs = "\n".join(d.get("content", "")[:1500] for d in p.get("docs", [])[:5])
        out.append(f"## Claude project: {p.get('name')}\n{p.get('description', '')}\n{docs}"[:per_conv_chars])
    for c in convs:
        msgs = _messages(c)
        user_text = "\n".join(_text(m) for m in msgs if m.get("sender") in ("human", "user"))
        if len(msgs) < 4 or len(BUILD_HINTS.findall(user_text)) < 3:
            continue
        date = (c.get("created_at") or "")[:10]
        body = user_text[: per_conv_chars - 200]
        out.append(f"## Conversation: {c.get('name') or 'untitled'} ({date}, {len(msgs)} messages)\n{body}")
    return out


def batches(items: list[str], max_chars: int = 40_000) -> list[str]:
    out, cur, size = [], [], 0
    for item in items:
        if size + len(item) > max_chars and cur:
            out.append("\n\n".join(cur))
            cur, size = [], 0
        cur.append(item)
        size += len(item)
    if cur:
        out.append("\n\n".join(cur))
    return out
