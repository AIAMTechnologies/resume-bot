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
# Interstitials are short pages that say what they are in the title, a heading, or an alert. Job
# descriptions for security roles ("investigate unusual activity", "security verification tooling")
# must never count, so long pages are only judged by their headline text.
SHORT_PAGE_CHARS = 2500
HEADLINE_JS = """() => {
  const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
  const parts = [document.title, ...[...document.querySelectorAll('h1, h2, h3, [role=alert], [role=dialog]')]
    .slice(0, 40).map(e => e.innerText)];
  const body = clean(document.body ? document.body.innerText : '');
  return {headline: clean(parts.join(' | ')).slice(0, 4000), body: body.slice(0, 20000), length: body.length};
}"""


def challenge_kind(headline: str, body: str, body_length: int) -> str | None:
    """Pure check used by check_page: a kind name, or None when the text looks like a normal page."""
    for pat, kind in TEXT_PATTERNS:
        if pat.search(headline or ""):
            return kind
        if body_length <= SHORT_PAGE_CHARS and pat.search(body or ""):
            return kind
    return None


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
        info = await page.evaluate(HEADLINE_JS)
    except Exception:
        return
    kind = challenge_kind(info.get("headline", ""), info.get("body", ""), int(info.get("length", 0)))
    if kind:
        raise ChallengeDetected(kind, url)
