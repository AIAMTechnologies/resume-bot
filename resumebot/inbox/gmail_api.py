"""Gmail API access with OAuth (no app password needed).

One-time setup: `resumebot gmail-auth` opens Google's consent screen; the refresh token is
kept in data/gmail_token.json. Scope is read-only.
"""
from __future__ import annotations

import base64
import re
from pathlib import Path

import httpx

from ..config import DATA_DIR, env

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CLIENT_FILE = DATA_DIR / "gmail_client.json"
TOKEN_FILE = DATA_DIR / "gmail_token.json"
API = "https://gmail.googleapis.com/gmail/v1/users/me"


def configured() -> bool:
    return TOKEN_FILE.exists()


def authorize(client_file: Path | None = None) -> str:
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(client_file or CLIENT_FILE), SCOPES)
    creds = flow.run_local_server(port=0, login_hint=env().gmail_address or None,
                                  prompt="consent", access_type="offline")
    TOKEN_FILE.write_text(creds.to_json())
    TOKEN_FILE.chmod(0o600)
    return profile(creds.token)["emailAddress"]


class NeedsReauth(Exception):
    """Refresh token expired or revoked (Google expires them weekly for apps in Testing mode)."""


def _token() -> str:
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise NeedsReauth(str(e)) from e
        TOKEN_FILE.write_text(creds.to_json())
    return creds.token


def profile(token: str | None = None) -> dict:
    r = httpx.get(f"{API}/profile", headers={"Authorization": f"Bearer {token or _token()}"}, timeout=30)
    r.raise_for_status()
    return r.json()


def _body(payload: dict) -> str:
    texts, htmls = [], []

    def walk(part: dict) -> None:
        data = (part.get("body") or {}).get("data")
        if data:
            decoded = base64.urlsafe_b64decode(data + "===").decode(errors="ignore")
            (texts if part.get("mimeType") == "text/plain" else htmls).append(decoded)
        for p in part.get("parts") or []:
            walk(p)

    walk(payload)
    text = "\n".join(texts)
    if not text and htmls:
        from bs4 import BeautifulSoup
        text = BeautifulSoup("\n".join(htmls), "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", text)[:6000]


def fetch_since(after_ms: int, limit: int = 50) -> list[tuple[int, str, str, str]]:
    """[(internal_date_ms, from, subject, body)] for inbox mail after `after_ms`, oldest first."""
    token = _token()
    headers = {"Authorization": f"Bearer {token}"}
    q = f"in:inbox after:{after_ms // 1000}" if after_ms else "in:inbox newer_than:1d"
    with httpx.Client(headers=headers, timeout=30) as c:
        r = c.get(f"{API}/messages", params={"q": q, "maxResults": limit})
        r.raise_for_status()
        out = []
        for m in r.json().get("messages", []):
            msg = c.get(f"{API}/messages/{m['id']}", params={"format": "full"}).json()
            ts = int(msg.get("internalDate", 0))
            if ts <= after_ms:
                continue
            hdrs = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
            out.append((ts, hdrs.get("from", ""), hdrs.get("subject", ""), _body(msg.get("payload", {}))))
    return sorted(out)
