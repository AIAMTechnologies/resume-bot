"""Greenhouse, Lever, and Ashby: public job-board APIs for discovery, browser for applying.

These are the lowest-risk sources: discovery is plain JSON (no login, no scraping) and the
application forms are the company's own.
"""
from __future__ import annotations

import asyncio
import random
import re
from datetime import datetime, timezone

import httpx

from .. import db
from ..browser import guards
from ..config import companies
from .base import ApplyContext, ApplyResult, JobData, Source, strip_html
from .forms import click_button, fill_form, page_has_text

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"}


def _ts(value) -> datetime | None:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except ValueError:
        return None



COOKIE_DECLINE_RE = re.compile(r"^(necessary only|only necessary|reject( all)?|decline( all)?|deny|essential only|use necessary cookies only)$", re.I)


async def dismiss_cookies(ctx: ApplyContext) -> None:
    """Close a cookie banner the privacy-preserving way (necessary cookies only), if one is showing."""
    try:
        for button in await ctx.page.get_by_role("button").all():
            if await button.is_visible() and COOKIE_DECLINE_RE.match((await button.inner_text()).strip()):
                await ctx.human.click(button)
                return
    except Exception:  # noqa: BLE001 — a banner is never worth failing an application over
        pass

class _BoardSource(Source):
    async def _fetch_board(self, client: httpx.AsyncClient, slug: str) -> list[JobData]:
        raise NotImplementedError

    async def discover(self, titles: list[str]) -> list[JobData]:
        slugs = companies().get(self.name, [])
        out: list[JobData] = []
        async with httpx.AsyncClient(headers=UA, timeout=30, follow_redirects=True) as client:
            for slug in slugs:
                try:
                    out.extend(await self._fetch_board(client, slug))
                except httpx.HTTPStatusError as e:
                    db.log(f"Board '{slug}' returned {e.response.status_code} — check the slug in "
                           f"config/companies.yaml", level="warning", source=self.name, kind="discover")
                except Exception as e:  # noqa: BLE001
                    db.log(f"Board '{slug}' failed: {e}", level="warning", source=self.name, kind="discover")
                await asyncio.sleep(random.uniform(0.5, 2.0))  # be polite to the API
        return out

    async def _open_and_read(self, ctx: ApplyContext, url: str) -> None:
        try:
            await ctx.page.goto(url, wait_until="domcontentloaded")
        except Exception as e:  # noqa: BLE001 — slow page load: one patient retry
            if "Timeout" not in type(e).__name__:
                raise
            await ctx.human.pause(5, 10)
            await ctx.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await ctx.human.pause(1.5, 3.5)
        await dismiss_cookies(ctx)
        await guards.check_page(ctx.page)
        await ctx.human.read(ctx.job.description)

    async def _submit_and_confirm(self, ctx: ApplyContext, submit_names: list[str], success: str) -> ApplyResult:
        if ctx.dry_run:
            return ApplyResult(False, "dry run: form filled, not submitted")
        await ctx.human.pause(1.5, 4.0)  # "reviewing" before submit
        if not await click_button(ctx, *submit_names):
            return ApplyResult(False, "submit button not found")
        await ctx.human.pause(2, 4)
        await guards.check_page(ctx.page)
        if await page_has_text(ctx, success, timeout=25):
            return ApplyResult(True, "confirmation page seen")
        # Validation errors keep us on the form.
        errors = await ctx.page.locator("[class*=error i]:visible, [role=alert]:visible").all_inner_texts()
        errors = [e.strip() for e in errors if e.strip()]
        return ApplyResult(False, "no confirmation; " + ("; ".join(errors[:5]) if errors else "unknown state"))


class Greenhouse(_BoardSource):
    name = "greenhouse"

    async def _fetch_board(self, client, slug):
        r = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})
        r.raise_for_status()
        company = slug.replace("-", " ").title()
        out = []
        for j in r.json().get("jobs", []):
            company = j.get("company_name") or company
            loc = (j.get("location") or {}).get("name", "")
            out.append(JobData(
                source=self.name, external_id=f"{slug}:{j['id']}", company=company, title=j["title"],
                url=j.get("absolute_url", ""), location=loc, remote="remote" in loc.lower(),
                apply_url=f"https://job-boards.greenhouse.io/embed/job_app?for={slug}&token={j['id']}",
                description=strip_html(j.get("content", "")), posted_at=_ts(j.get("updated_at")),
            ))
        return out

    async def apply(self, ctx: ApplyContext) -> ApplyResult:
        await self._open_and_read(ctx, ctx.job.apply_url or ctx.job.url)
        form = "#application-form, #application_form, form"
        answers = await fill_form(ctx, form)
        res = await self._submit_and_confirm(ctx, ["submit application", "^submit$", "apply"],
                                             r"thanks? (you |so much )?for (applying|your application|your interest)|application (has )?(successfully )?(been )?(received|submitted)|we.ve received your application")
        res.extra["answers"] = answers
        return res


class Lever(_BoardSource):
    name = "lever"

    async def _fetch_board(self, client, slug):
        r = await client.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
        r.raise_for_status()
        out = []
        for j in r.json():
            cats = j.get("categories") or {}
            lists = "\n".join(f"{l.get('text')}\n{strip_html(l.get('content', ''))}" for l in j.get("lists", []))
            desc = "\n\n".join(x for x in [j.get("descriptionPlain", ""), lists, j.get("additionalPlain", "")] if x)
            loc = cats.get("location", "") or ", ".join(cats.get("allLocations", []) or [])
            sal = j.get("salaryRange") or {}
            out.append(JobData(
                source=self.name, external_id=f"{slug}:{j['id']}", company=slug.replace("-", " ").title(),
                title=j.get("text", ""), url=j.get("hostedUrl", ""), apply_url=j.get("applyUrl", ""),
                location=loc, remote=(j.get("workplaceType") == "remote") or "remote" in loc.lower(),
                employment_type=cats.get("commitment", ""), description=desc,
                salary=f"{sal.get('min')}-{sal.get('max')} {sal.get('currency', '')}" if sal else "",
                posted_at=_ts(j.get("createdAt")),
            ))
        return out

    async def apply(self, ctx: ApplyContext) -> ApplyResult:
        await self._open_and_read(ctx, ctx.job.url)
        if not await click_button(ctx, "apply for this job", "^apply$"):
            await ctx.page.goto(ctx.job.apply_url, wait_until="domcontentloaded")
        await ctx.human.pause(1.5, 3)
        await guards.check_page(ctx.page)
        answers = await fill_form(ctx, "form#application-form, form")
        res = await self._submit_and_confirm(ctx, ["submit application", "^submit"],
                                             r"application submitted|thanks for applying|we.ve received your application")
        res.extra["answers"] = answers
        return res


class Ashby(_BoardSource):
    name = "ashby"

    async def _fetch_board(self, client, slug):
        r = await client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                             params={"includeCompensation": "true"})
        r.raise_for_status()
        out = []
        for j in r.json().get("jobs", []):
            if not j.get("isListed", True):
                continue
            comp = (j.get("compensation") or {}).get("compensationTierSummary", "")
            loc = j.get("location", "")
            out.append(JobData(
                source=self.name, external_id=f"{slug}:{j['id']}", company=slug.replace("-", " ").title(),
                title=j.get("title", ""), url=j.get("jobUrl", ""),
                apply_url=j.get("applyUrl") or (j.get("jobUrl", "") + "/application"),
                location=loc, remote=bool(j.get("isRemote")) or "remote" in loc.lower(),
                employment_type=j.get("employmentType", ""),
                description=j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", "")),
                salary=comp, posted_at=_ts(j.get("publishedAt")),
            ))
        return out

    async def apply(self, ctx: ApplyContext) -> ApplyResult:
        await self._open_and_read(ctx, ctx.job.url)
        clicked = await click_button(ctx, "apply for this job", "^application$")
        await ctx.human.pause(1, 2)
        if not clicked or "/application" not in ctx.page.url:  # a banner can swallow the click
            await ctx.page.goto(ctx.job.apply_url or ctx.job.url.rstrip("/") + "/application",
                                wait_until="domcontentloaded")
            await dismiss_cookies(ctx)
        await ctx.human.pause(1.5, 3)
        await guards.check_page(ctx.page)
        answers = await fill_form(ctx, "form, [class*=application-form]")
        res = await self._submit_and_confirm(ctx, ["submit application", "^submit"],
                                             r"thanks? (you |so much )?for (applying|your application|your interest)|application (was |has been )?(successfully )?(submitted|received)|successfully submitted")
        res.extra["answers"] = answers
        return res
