"""One real Chrome window, one persistent profile — like one person with one browser.

The profile lives in data/profiles/main. `import_chrome_profile` seeds it from one of your
existing Chrome profiles (e.g. the "ResumeBot" profile signed in to your application Gmail),
so Google sign-in and site cookies carry over.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from ..config import DATA_DIR, settings

PROFILE_DIR = DATA_DIR / "profiles" / "main"
BLOCKED_HOSTS = re.compile(r"^https?://([a-z0-9-]+\.)*linkedin\.com(/|$)", re.I)
CHROME_DATA = Path.home() / "Library/Application Support/Google/Chrome"
LOGIN_URLS = {
    "indeed": "https://secure.indeed.com/auth",
    "google": "https://accounts.google.com",
}
# Caches and session-restore data aren't worth copying (and Sessions would reopen old tabs).
SKIP_COPY = {"Cache", "Code Cache", "GPUCache", "Service Worker", "Sessions", "Session Storage",
             "DawnGraphiteCache", "DawnWebGPUCache", "GrShaderCache", "ShaderCache", "blob_storage",
             "optimization_guide_model_store", "Download Service"}


def chrome_profiles() -> list[dict]:
    """Your Chrome profiles: [{dir, name, email}]."""
    state = CHROME_DATA / "Local State"
    if not state.exists():
        return []
    cache = json.loads(state.read_text()).get("profile", {}).get("info_cache", {})
    return [{"dir": d, "name": v.get("name", ""), "email": v.get("user_name", "")} for d, v in cache.items()]


def import_chrome_profile(profile: str) -> Path:
    """Copy a Chrome profile (by folder, display name, or email) into the bot's profile."""
    match = next((p for p in chrome_profiles() if profile in (p["dir"], p["name"], p["email"])), None)
    if not match:
        raise ValueError(f"No Chrome profile '{profile}'. Found: " +
                         ", ".join(f"{p['name']} ({p['dir']})" for p in chrome_profiles()))
    src = CHROME_DATA / match["dir"]
    dest = PROFILE_DIR / "Default"
    if dest.exists():
        shutil.rmtree(dest)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, ignore=lambda d, names: [n for n in names if n in SKIP_COPY],
                    ignore_dangling_symlinks=True)
    return dest


# No account to protect on these, so applications may run side by side in separate tabs.
OWN_TAB_SOURCES = {"greenhouse", "lever", "ashby"}


class BrowserManager:
    """Keeps one persistent Chrome context open; `lock` serialises all browser work."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self._launching = asyncio.Lock()  # parallel tabs must not launch Chrome twice
        self._pw: Playwright | None = None
        self._ctx: BrowserContext | None = None

    async def _launch(self) -> BrowserContext:
        if self._pw is None:
            self._pw = await async_playwright().start()
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        ctx = await self._pw.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            channel="chrome",           # your installed Google Chrome, not bundled Chromium
            headless=os.environ.get("RESUMEBOT_HEADLESS") == "1",
            no_viewport=True,           # real window size, like a person's browser
            locale="en-CA",
            timezone_id=settings().timezone,
            args=["--disable-blink-features=AutomationControlled", "--window-size=1440,900",
                  "--no-first-run", "--no-default-browser-check"],
            ignore_default_args=["--enable-automation"],
        )
        ctx.on("close", lambda _: self._forget())
        # Hard safety lock: automated browsing never touches LinkedIn (account protection).
        await ctx.route(BLOCKED_HOSTS, lambda route: route.abort("blockedbyclient"))
        return ctx

    def _forget(self) -> None:
        self._ctx = None

    async def context(self) -> BrowserContext:
        async with self._launching:
            if self._ctx is None:
                self._ctx = await self._launch()
            return self._ctx

    @asynccontextmanager
    async def page(self, source: str = "") -> AsyncIterator[Page]:
        """Exclusive use of the browser, reusing the first tab like a person would.
        Company application portals get a tab of their own instead, so several can run at once."""
        if source in OWN_TAB_SOURCES:
            async with self.tab() as page:
                yield page
            return
        async with self.lock:
            ctx = await self.context()
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            yield page

    @asynccontextmanager
    async def tab(self) -> AsyncIterator[Page]:
        """A tab of its own, for company application portals (no account to protect), so several
        applications can be filled at once. Never used for LinkedIn or other sign-in sources."""
        ctx = await self.context()
        page = await ctx.new_page()
        try:
            yield page
        finally:
            try:
                await page.close()
            except Exception:  # noqa: BLE001 — browser may already be gone
                pass

    async def close(self) -> None:
        if self._ctx is not None:
            try:
                await self._ctx.close()
            except Exception:
                pass
        self._ctx = None

    async def shutdown(self) -> None:
        await self.close()
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None


browsers = BrowserManager()


async def interactive_login(source: str) -> None:
    """Open the bot's browser so you can log in by hand. Returns when you close the window."""
    mgr = BrowserManager()
    ctx = await mgr.context()
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    await page.goto(LOGIN_URLS.get(source, "about:blank"))
    closed = asyncio.Event()
    ctx.on("close", lambda _: closed.set())
    page.on("close", lambda _: closed.set())
    await closed.wait()
    await mgr.shutdown()
