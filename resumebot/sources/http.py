"""Browser-like HTTP headers for the plain-HTTP fetches (public job boards, LinkedIn guest pages).

Every module used to send a different, partial header set with a different Chrome version, which
is an easy fingerprint. This mirrors the installed Chrome's version (read from the app bundle on
macOS) and the client-hint / fetch-metadata headers Chrome sends, so the bot's HTTP traffic looks
like the same browser that fills the forms.
"""
from __future__ import annotations

import plistlib
import re
from functools import lru_cache
from pathlib import Path

DEFAULT_CHROME_MAJOR = "131"
CHROME_PLIST = Path("/Applications/Google Chrome.app/Contents/Info.plist")


@lru_cache
def chrome_major() -> str:
    try:
        version = plistlib.loads(CHROME_PLIST.read_bytes()).get("CFBundleShortVersionString", "")
        major = re.match(r"\d+", str(version))
        if major and int(major.group()) >= 100:
            return major.group()
    except Exception:  # noqa: BLE001 — not macOS, or no Chrome: use a current default
        pass
    return DEFAULT_CHROME_MAJOR


def user_agent() -> str:
    return (f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome_major()}.0.0.0 Safari/537.36")


def headers(kind: str = "html", referer: str = "") -> dict[str, str]:
    """kind: 'html' for page/fragment fetches, 'json' for public JSON APIs."""
    major = chrome_major()
    common = {
        "User-Agent": user_agent(),
        "Accept-Language": "en-CA,en-US;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "sec-ch-ua": f'"Google Chrome";v="{major}", "Chromium";v="{major}", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    }
    if kind == "json":
        return {**common, "Accept": "application/json, text/plain, */*",
                "sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "cross-site"}
    out = {**common, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
           "Upgrade-Insecure-Requests": "1", "sec-fetch-dest": "document", "sec-fetch-mode": "navigate",
           "sec-fetch-site": "same-origin" if referer else "none", "sec-fetch-user": "?1"}
    if referer:
        out["Referer"] = referer
    return out
