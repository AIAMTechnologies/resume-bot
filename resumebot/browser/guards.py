"""Detect CAPTCHAs, checkpoints, and account warnings. The bot never solves these — it stops."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page


class ChallengeDetected(Exception):
    def __init__(self, kind: str, url: str):
        super().__init__(f"{kind} at {url}")
        self.kind = kind
        self.url = url


class NotLoggedIn(Exception):
    pass


URL_PATTERNS = [
    (re.compile(r"/checkpoint/"), "linkedin checkpoint"),
    (re.compile(r"/authwall"), "authwall"),
    (re.compile(r"captcha", re.I), "captcha"),
    (re.compile(r"challenges\.cloudflare\.com|/cdn-cgi/challenge"), "cloudflare challenge"),
]
TEXT_PATTERNS = [
    (re.compile(r"unusual activity|suspicious activity", re.I), "unusual activity warning"),
    (re.compile(r"verify (that )?you('| a)re (a )?human|are you a robot", re.I), "human verification"),
    (re.compile(r"let'?s do a quick security check|security verification", re.I), "security check"),
    (re.compile(r"your account has been (restricted|temporarily limited)", re.I), "account restricted"),
    (re.compile(r"you('|’)ve reached the (weekly|daily) (application )?limit", re.I), "rate limit"),
]
# Invisible reCAPTCHA v3 badges are everywhere and harmless; only visible challenges count.
VISIBLE_CHALLENGE_SELECTORS = [
    "iframe[src*='recaptcha/api2/bframe']", "iframe[title*='challenge' i]",
    "iframe[src*='hcaptcha.com'][src*='challenge']", "#challenge-stage", "iframe[src*='arkoselabs']",
    "iframe[src*='funcaptcha']", "iframe[src*='challenges.cloudflare.com']",
]


async def check_page(page: "Page") -> None:
    url = page.url
    for pat, kind in URL_PATTERNS:
        if pat.search(url):
            raise ChallengeDetected(kind, url)
    for sel in VISIBLE_CHALLENGE_SELECTORS:
        loc = page.locator(sel)
        if await loc.count() and await loc.first.is_visible():
            raise ChallengeDetected(f"visible challenge ({sel})", url)
    try:
        body = (await page.locator("body").inner_text(timeout=3000))[:20000]
    except Exception:
        return
    for pat, kind in TEXT_PATTERNS:
        if pat.search(body):
            raise ChallengeDetected(kind, url)
