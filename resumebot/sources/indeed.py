"""Indeed: browser discovery + Indeed Apply. External "apply on company site" jobs are rerouted."""
from __future__ import annotations

import random
import re
from urllib.parse import quote_plus

from ..browser import guards
from ..browser.human import Human
from ..browser.session import browsers
from ..config import settings
from .base import ApplyContext, ApplyResult, JobData, Source
from .forms import click_button, fill_form, page_has_text
from .routing import Rerouted

BASE = "https://ca.indeed.com"
SEARCH = BASE + "/jobs?q={q}&l={loc}&fromage=14&sort=date"
CARD = "div.job_seen_beacon, td.resultContent"
DESC = "#jobDescriptionText"
APPLY_INDEED = "#indeedApplyButton, button[aria-label*='Apply now' i], button:has-text('Apply now')"
APPLY_EXTERNAL = "button[aria-label*='company site' i], a:has-text('Apply on company site'), button:has-text('Apply on company site')"


async def _text(page, selector: str) -> str:
    loc = page.locator(selector).first
    try:
        return (await loc.inner_text(timeout=4000)).strip() if await loc.count() else ""
    except Exception:
        return ""


class Indeed(Source):
    name = "indeed"
    uses_browser_for_discovery = True
    max_views_per_discovery = 20

    async def discover(self, titles: list[str]) -> list[JobData]:
        loc = settings().targets.locations
        places = (["Remote"] if loc.remote else []) + [c for c in loc.cities[:2]]
        picks = random.sample(titles, k=min(2, len(titles))) if titles else ["software engineer"]
        combos = [(t, p) for t in picks for p in places]
        random.shuffle(combos)
        out: list[JobData] = []
        views = 0
        async with browsers.page(self.name) as page:
            human = Human(page)
            for title, place in combos[:3]:
                await page.goto(SEARCH.format(q=quote_plus(title), loc=quote_plus(place)), wait_until="domcontentloaded")
                await human.pause(2.5, 5)
                await guards.check_page(page)
                cards = page.locator(CARD)
                for i in range(min(await cards.count(), 12)):
                    if views >= self.max_views_per_discovery:
                        break
                    card = cards.nth(i)
                    link = card.locator("a[data-jk], h2.jobTitle a").first
                    if not await link.count():
                        continue
                    jk = await link.get_attribute("data-jk")
                    if not jk:
                        m = re.search(r"jk=([a-f0-9]+)", await link.get_attribute("href") or "")
                        jk = m.group(1) if m else None
                    if not jk:
                        continue
                    await human.click(link)
                    await human.pause(1.5, 3.5)
                    views += 1
                    await guards.check_page(page)
                    desc = await _text(page, DESC)
                    await human.scroll(random.randint(150, 450))
                    location = await _text(card, "[data-testid=text-location], .companyLocation")
                    out.append(JobData(
                        source=self.name, external_id=jk,
                        title=(await link.inner_text()).strip(),
                        company=await _text(card, "[data-testid=company-name], .companyName"),
                        url=f"{BASE}/viewjob?jk={jk}", location=location,
                        remote="remote" in location.lower(), description=desc,
                        salary=await _text(card, "[data-testid=attribute_snippet_testid], .salary-snippet-container"),
                        easy_apply=await page.locator(APPLY_INDEED).count() > 0,
                    ))
                    await human.pause(1, 4)
                await human.pause(5, 20)
        return out

    async def browse_only(self, page, human, job) -> None:
        await page.goto(job.url, wait_until="domcontentloaded")
        await human.pause(2, 4)
        await guards.check_page(page)
        await human.read(job.description)

    async def apply(self, ctx: ApplyContext) -> ApplyResult:
        page, human = ctx.page, ctx.human
        await page.goto(ctx.job.url, wait_until="domcontentloaded")
        await human.pause(2, 5)
        await guards.check_page(page)
        await human.read(ctx.job.description)

        ext = page.locator(APPLY_EXTERNAL).first
        if await ext.count() and not await page.locator(APPLY_INDEED).count():
            href = await ext.get_attribute("href")
            if href:
                raise Rerouted(href if href.startswith("http") else BASE + href)
            async with page.context.expect_page(timeout=15000) as popup:
                await human.click(ext)
            new = await popup.value
            await new.wait_for_load_state("domcontentloaded")
            url = new.url
            await new.close()
            raise Rerouted(url)

        btn = page.locator(APPLY_INDEED).first
        if not await btn.count():
            return ApplyResult(False, "apply button not found")
        target = page
        try:
            async with page.context.expect_page(timeout=6000) as popup:
                await human.click(btn)
            target = await popup.value
            await target.wait_for_load_state("domcontentloaded")
        except Exception:
            pass  # opened in the same tab
        ctx.page = target
        ctx.human = Human(target) if target is not page else human
        answers: dict[str, str] = {}
        for _ in range(12):
            await human.pause(1.5, 3)
            await guards.check_page(target)
            if re.search(r"/auth|login", target.url):
                raise guards.NotLoggedIn("indeed")
            answers.update(await fill_form(ctx, "main, form, body"))
            submit = target.get_by_role("button", name=re.compile(r"submit (your )?application", re.I))
            if await submit.count():
                if ctx.dry_run:
                    return ApplyResult(False, "dry run: reached submit", extra={"answers": answers})
                await ctx.human.pause(1.5, 4)
                ctx.submit_clicked = True
                await ctx.human.click(submit.first)
                ok = await page_has_text(ctx, r"application has been submitted|you.ve applied|application submitted", 25)
                return ApplyResult(ok, "Indeed Apply submitted" if ok else "no confirmation seen",
                                   extra={"answers": answers})
            if not await click_button(ctx, "^continue", "review your application", "^next$"):
                return ApplyResult(False, "stuck: no continue/submit button", extra={"answers": answers})
        return ApplyResult(False, "too many steps", extra={"answers": answers})
