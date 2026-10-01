"""Human-like input: curved mouse paths, variable typing, reading pauses.

The math helpers (bezier_path, key_delays, reading_seconds) are pure and unit-tested.
`Human` wraps a Playwright page and uses them.
"""
from __future__ import annotations

import asyncio
import math
import random
from typing import TYPE_CHECKING

from ..config import settings

if TYPE_CHECKING:
    from playwright.async_api import Locator, Page

NEIGHBOURS = {
    "a": "qwsz", "b": "vghn", "c": "xdfv", "d": "serfcx", "e": "wsdr", "f": "drtgvc", "g": "ftyhbv",
    "h": "gyujnb", "i": "ujko", "j": "huikmn", "k": "jiolm", "l": "kop", "m": "njk", "n": "bhjm",
    "o": "iklp", "p": "ol", "q": "wa", "r": "edft", "s": "awedxz", "t": "rfgy", "u": "yhji",
    "v": "cfgb", "w": "qase", "x": "zsdc", "y": "tghu", "z": "asx",
}


# ---------- pure helpers ----------

def _ease(t: float) -> float:
    # ease-in-out: slow start, fast middle, slow finish (like a real hand)
    return 0.5 - 0.5 * math.cos(math.pi * t)


def bezier_path(start: tuple[float, float], end: tuple[float, float], rng: random.Random | None = None
                ) -> list[tuple[float, float]]:
    """Cubic Bézier path from start to end with randomized control points and jitter."""
    rng = rng or random
    (x0, y0), (x3, y3) = start, end
    dist = math.hypot(x3 - x0, y3 - y0)
    spread = max(20.0, dist * 0.35)
    x1, y1 = x0 + (x3 - x0) * rng.uniform(0.2, 0.4) + rng.uniform(-spread, spread), \
        y0 + (y3 - y0) * rng.uniform(0.2, 0.4) + rng.uniform(-spread, spread)
    x2, y2 = x0 + (x3 - x0) * rng.uniform(0.6, 0.8) + rng.uniform(-spread, spread), \
        y0 + (y3 - y0) * rng.uniform(0.6, 0.8) + rng.uniform(-spread, spread)
    steps = max(12, min(90, int(dist / rng.uniform(8, 14))))
    pts = []
    for i in range(1, steps + 1):
        t = _ease(i / steps)
        mt = 1 - t
        x = mt**3 * x0 + 3 * mt**2 * t * x1 + 3 * mt * t**2 * x2 + t**3 * x3
        y = mt**3 * y0 + 3 * mt**2 * t * y1 + 3 * mt * t**2 * y2 + t**3 * y3
        if i < steps:  # jitter everywhere except the landing point
            x += rng.gauss(0, 0.8)
            y += rng.gauss(0, 0.8)
        pts.append((x, y))
    return pts


def key_delays(text: str, wpm: tuple[int, int], rng: random.Random | None = None) -> list[float]:
    """Seconds to wait before each keystroke. Log-normal around the chosen WPM with word pauses."""
    rng = rng or random
    cps = rng.uniform(*wpm) * 5 / 60  # chars per second
    mean = 1 / cps
    sigma = 0.35
    mu = math.log(mean) - sigma**2 / 2
    delays = []
    for i, ch in enumerate(text):
        d = rng.lognormvariate(mu, sigma)
        if ch == " ":
            d *= rng.uniform(1.1, 1.8)
        if ch in ".,;:!?\n":
            d *= rng.uniform(1.5, 3.0)
        if i and i % rng.randint(25, 60) == 0:
            d += rng.uniform(0.3, 1.2)  # a thinking pause
        delays.append(min(d, 2.5))
    return delays


def reading_seconds(text: str, reading_wpm: tuple[int, int], cap: float = 90.0,
                    rng: random.Random | None = None) -> float:
    rng = rng or random
    words = max(1, len(text.split()))
    # People skim job posts: read ~35-70% of it.
    return min(cap, words * rng.uniform(0.35, 0.7) / rng.uniform(*reading_wpm) * 60)


# ---------- browser actor ----------

class Human:
    def __init__(self, page: "Page", brisk: bool = False):
        """brisk: company application portals — shorter reading and quicker typing (no account to protect)."""
        self.page = page
        self.cfg = settings().human
        self.brisk = brisk
        vp = page.viewport_size or {"width": 1280, "height": 800}
        self.x, self.y = random.uniform(0, vp["width"]), random.uniform(0, vp["height"])

    async def pause(self, a: float = 0.4, b: float = 1.4) -> None:
        await asyncio.sleep(random.uniform(a, b))

    async def move_to(self, x: float, y: float) -> None:
        if random.random() < 0.12:  # overshoot then correct
            ox, oy = x + random.uniform(-25, 25), y + random.uniform(-12, 12)
            for px, py in bezier_path((self.x, self.y), (ox, oy)):
                await self.page.mouse.move(px, py)
                await asyncio.sleep(random.uniform(0.004, 0.014))
            self.x, self.y = ox, oy
            await asyncio.sleep(random.uniform(0.05, 0.2))
        for px, py in bezier_path((self.x, self.y), (x, y)):
            await self.page.mouse.move(px, py)
            await asyncio.sleep(random.uniform(0.004, 0.016))
        self.x, self.y = x, y

    async def _target(self, locator: "Locator") -> tuple[float, float]:
        await locator.scroll_into_view_if_needed()
        await self.pause(0.15, 0.5)
        box = await locator.bounding_box()
        if not box:
            raise RuntimeError("element has no bounding box")
        # Aim near the centre, gaussian spread, clamped inside the element.
        x = box["x"] + box["width"] * min(0.9, max(0.1, random.gauss(0.5, 0.15)))
        y = box["y"] + box["height"] * min(0.85, max(0.15, random.gauss(0.5, 0.15)))
        return x, y

    async def hover(self, locator: "Locator") -> None:
        await self.move_to(*await self._target(locator))

    async def click(self, locator: "Locator") -> None:
        x, y = await self._target(locator)
        await self.move_to(x, y)
        await self.pause(0.08, 0.35)
        await self.page.mouse.down()
        await asyncio.sleep(random.uniform(0.05, 0.14))
        await self.page.mouse.up()
        await self.pause(0.2, 0.7)

    async def type(self, locator: "Locator", text: str, clear: bool = True, typos: bool = True) -> None:
        await self.click(locator)
        if clear:
            await self.page.keyboard.press("ControlOrMeta+A")  # ⌘A on macOS, Ctrl+A elsewhere
            await self.page.keyboard.press("Backspace")
            await self.pause(0.1, 0.4)
        if len(text) > 350:
            # Nobody hand-types a cover letter into a form; they paste it.
            await self.page.keyboard.insert_text(text)
            await self.pause(0.5, 1.5)
            return
        delays = key_delays(text, (110, 150) if self.brisk else self.cfg.wpm)
        typo_every_char = self.cfg.typo_rate / 5 if typos else 0
        for ch, d in zip(text, delays):
            await asyncio.sleep(d)
            low = ch.lower()
            if typo_every_char and low in NEIGHBOURS and random.random() < typo_every_char:
                await self.page.keyboard.type(random.choice(NEIGHBOURS[low]))
                await asyncio.sleep(random.uniform(0.15, 0.5))
                await self.page.keyboard.press("Backspace")
                await asyncio.sleep(random.uniform(0.08, 0.25))
            await self.page.keyboard.type(ch)
        await self.pause(0.2, 0.6)

    async def scroll(self, pixels: int) -> None:
        remaining = pixels
        while abs(remaining) > 0:
            step = int(math.copysign(min(abs(remaining), random.randint(60, 160)), remaining))
            await self.page.mouse.wheel(0, step)
            remaining -= step
            await asyncio.sleep(random.uniform(0.03, 0.12))
        await self.pause(0.2, 0.8)

    async def read(self, text: str) -> None:
        """Scroll through content for about as long as a person would spend reading it."""
        total = reading_seconds(text, self.cfg.reading_wpm, cap=12.0 if self.brisk else 90.0)
        spent = 0.0
        while spent < total:
            chunk = random.uniform(2.0, 6.0)
            await asyncio.sleep(chunk)
            spent += chunk
            await self.scroll(random.randint(150, 450) * (1 if random.random() > 0.1 else -1))
            if random.random() < 0.2:
                vp = self.page.viewport_size or {"width": 1280, "height": 800}
                await self.move_to(random.uniform(100, vp["width"] - 100), random.uniform(100, vp["height"] - 100))

    async def idle(self, seconds: float) -> None:
        """Look busy-but-idle: small mouse drifts and scrolls."""
        end = asyncio.get_event_loop().time() + seconds
        vp = self.page.viewport_size or {"width": 1280, "height": 800}
        while asyncio.get_event_loop().time() < end:
            if random.random() < 0.5:
                await self.move_to(random.uniform(50, vp["width"] - 50), random.uniform(50, vp["height"] - 50))
            else:
                await self.scroll(random.randint(-200, 300))
            await asyncio.sleep(random.uniform(1.5, 5))

    async def select(self, locator: "Locator", label: str) -> None:
        await self.click(locator)
        await locator.select_option(label=label)
        await self.pause()

    async def check(self, locator: "Locator") -> None:
        if not await locator.is_checked():
            await self.click(locator)

    async def upload(self, locator: "Locator", path: str) -> None:
        await self.pause(0.8, 2.0)  # "finding the file"
        await locator.set_input_files(path)
        await self.pause(1.0, 2.5)
