"""SmartRecruiters, JazzHR, Workable, and Recruitee: public job boards.

Discovery is public JSON or HTML (no login); application is browser-based
through the company's own career-page forms — exactly like Greenhouse, Lever,
and Ashby in ats_boards.py.  All four extend _BoardSource so they plug into
the same pipeline: multi-lane parallel tabs, pacing, review, etc.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

import httpx

from .. import db
from .ats_boards import _BoardSource, dismiss_cookies, CLOSED_RE, CLOSED_URL_RE
from .base import ApplyContext, ApplyResult, JobData, ManualRequired, PostingClosed, strip_html
from .forms import click_button, fill_form, page_has_text


# ── helpers ──────────────────────────────────────────────────────────────

def _iso_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _loc_str(loc: dict) -> str:
    parts = [loc.get("city", ""), loc.get("region", ""), loc.get("country", "")]
    return ", ".join(p for p in parts if p)


# ── SmartRecruiters ──────────────────────────────────────────────────────

class SmartRecruiters(_BoardSource):
    """Public Posting API for discovery, browser career-page forms for applying.

    Discovery:  GET https://api.smartrecruiters.com/v1/companies/{slug}/postings
                No auth required.  Returns paginated JSON of active postings.
    Apply:      The ``applyUrl`` from the API goes to the company's SmartRecruiters
                career page.  The form is clean HTML — no CAPTCHA, no account needed.
    """
    name = "smartrecruiters"

    async def _fetch_board(self, client: httpx.AsyncClient, slug: str) -> list[JobData]:
        out: list[JobData] = []
        offset = 0
        limit = 100
        while True:
            r = await client.get(
                f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                params={"limit": limit, "offset": offset},
            )
            r.raise_for_status()
            data = r.json()
            for j in data.get("content", []):
                loc = j.get("location") or {}
                company_name = (j.get("company") or {}).get("name", slug.replace("-", " ").title())
                posting_id = j.get("uuid") or j.get("id", "")
                toe = j.get("typeOfEmployment") or {}
                out.append(JobData(
                    source=self.name,
                    external_id=f"{slug}:{posting_id}",
                    company=company_name,
                    title=j.get("name", ""),
                    url=j.get("ref", f"https://careers.smartrecruiters.com/{slug}/{posting_id}"),
                    apply_url=j.get("applyUrl", ""),
                    location=_loc_str(loc),
                    remote=bool(loc.get("remote")),
                    employment_type=toe.get("label", ""),
                    description="",  # list endpoint doesn't include full description
                    posted_at=_iso_ts(j.get("releasedDate")),
                ))
            total = data.get("totalFound", 0)
            offset += limit
            if offset >= total or not data.get("content"):
                break
        return out

    async def apply(self, ctx: ApplyContext) -> ApplyResult:
        url = ctx.job.apply_url or ctx.job.url
        # SmartRecruiters apply URLs redirect to the career page with the form.
        await self._open_and_read(ctx, url)
        # SmartRecruiters forms live inside a container with role "main" or a form tag.
        answers = await fill_form(ctx, "form, [role=main]")
        res = await self._submit_and_confirm(
            ctx,
            ["submit application", "submit your application", "^submit$", "apply", "apply now"],
            r"thanks? (you |so much )?for (applying|your application|your interest)|"
            r"application (has )?(successfully )?(been )?(received|submitted)|"
            r"we.ve received your application|thank you for your interest",
        )
        res.extra["answers"] = answers
        return res


# ── JazzHR ───────────────────────────────────────────────────────────────

_JAZZHR_JOB_RE = re.compile(
    r'<a[^>]+href="(https?://[^"]+\.applytojob\.com/apply/([^"/]+)[^"]*)"[^>]*>\s*'
    r'<span[^>]*class="[^"]*job-title[^"]*"[^>]*>([^<]+)</span>',
    re.I | re.S,
)
# Fallback pattern — some career pages use a simpler structure.
_JAZZHR_JOB_ALT_RE = re.compile(
    r'<a[^>]+href="(https?://[^"]+\.applytojob\.com/apply/([^"/]+)[^"]*)"[^>]*>([^<]+)</a>',
    re.I | re.S,
)
_JAZZHR_LOC_RE = re.compile(
    r'<span[^>]*class="[^"]*job-location[^"]*"[^>]*>([^<]+)</span>', re.I | re.S,
)


class JazzHR(_BoardSource):
    """JazzHR (applytojob.com): HTML career-page scrape for discovery, browser for applying.

    Discovery:  Fetch ``https://{slug}.applytojob.com`` and parse job links from the
                server-rendered HTML.  No JSON API is publicly available.
    Apply:      Individual job pages at ``/apply/{shortcode}`` have clean HTML forms.
                No CAPTCHA.
    """
    name = "jazzhr"

    async def _fetch_board(self, client: httpx.AsyncClient, slug: str) -> list[JobData]:
        r = await client.get(f"https://{slug}.applytojob.com")
        r.raise_for_status()
        html_text = r.text
        company = slug.replace("-", " ").title()

        # Extract job links from the career page HTML.
        matches = _JAZZHR_JOB_RE.findall(html_text) or _JAZZHR_JOB_ALT_RE.findall(html_text)
        seen: set[str] = set()
        out: list[JobData] = []
        for job_url, shortcode, title in matches:
            if shortcode in seen:
                continue
            seen.add(shortcode)
            # Try to find a location span near this job entry.
            loc_match = _JAZZHR_LOC_RE.search(html_text, html_text.find(job_url))
            location = loc_match.group(1).strip() if loc_match else ""
            out.append(JobData(
                source=self.name,
                external_id=f"{slug}:{shortcode}",
                company=company,
                title=title.strip(),
                url=job_url,
                apply_url=job_url,
                location=location,
                remote="remote" in location.lower(),
            ))
        return out

    async def apply(self, ctx: ApplyContext) -> ApplyResult:
        await self._open_and_read(ctx, ctx.job.apply_url or ctx.job.url)
        # JazzHR apply pages have the form directly on the page.
        answers = await fill_form(ctx, "form, #apply-form, .application-form")
        res = await self._submit_and_confirm(
            ctx,
            ["submit application", "^submit$", "apply", "apply now", "send application"],
            r"thanks? (you |so much )?for (applying|your application|your interest)|"
            r"application (has )?(successfully )?(been )?(received|submitted)|"
            r"we.ve received your application|your application has been sent",
        )
        res.extra["answers"] = answers
        return res


# ── Workable ─────────────────────────────────────────────────────────────

class Workable(_BoardSource):
    """Workable (apply.workable.com): public widget JSON API for discovery, browser for applying.

    Discovery:  GET https://apply.workable.com/api/v1/widget/accounts/{slug}
                No auth.  Returns JSON with all active job postings.
    Apply:      Job pages at ``https://apply.workable.com/{slug}/j/{shortcode}/``
                have clean HTML forms — Playwright-friendly, no aggressive anti-bot.
    """
    name = "workable"

    async def _fetch_board(self, client: httpx.AsyncClient, slug: str) -> list[JobData]:
        r = await client.get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
        r.raise_for_status()
        data = r.json()
        company = slug.replace("-", " ").title()
        out: list[JobData] = []
        for j in data.get("jobs", []):
            loc = j.get("location", "")
            shortcode = j.get("shortcode", "")
            job_url = j.get("url", f"https://apply.workable.com/{slug}/j/{shortcode}/")
            out.append(JobData(
                source=self.name,
                external_id=f"{slug}:{shortcode}",
                company=company,
                title=j.get("title", ""),
                url=job_url,
                apply_url=job_url,
                location=loc if isinstance(loc, str) else (loc.get("city", "") if isinstance(loc, dict) else ""),
                remote=j.get("workplace", "").lower() == "remote" or (
                    isinstance(loc, str) and "remote" in loc.lower()),
                employment_type=j.get("type", ""),
                description=j.get("description", ""),
                posted_at=_iso_ts(j.get("published_on") or j.get("created_at")),
            ))
        return out

    async def apply(self, ctx: ApplyContext) -> ApplyResult:
        await self._open_and_read(ctx, ctx.job.apply_url or ctx.job.url)
        # Workable job pages may have an "Apply" button to reveal the form.
        if not await page_has_text(ctx, r"resume|cover letter|first name|full name", timeout=3):
            await click_button(ctx, "apply for this job", "apply now", "^apply$")
            await ctx.human.pause(1.5, 3)
        answers = await fill_form(ctx, "form, .application-form, [data-ui=application]")
        res = await self._submit_and_confirm(
            ctx,
            ["submit application", "submit your application", "^submit$", "apply"],
            r"thanks? (you |so much )?for (applying|your application|your interest)|"
            r"application (has )?(successfully )?(been )?(received|submitted)|"
            r"we.ve received your application|successfully submitted|you.re all set",
        )
        res.extra["answers"] = answers
        return res


# ── Recruitee ────────────────────────────────────────────────────────────

class Recruitee(_BoardSource):
    """Recruitee ({slug}.recruitee.com): public offers API for discovery, browser for applying.

    Discovery:  GET https://{slug}.recruitee.com/api/offers/
                No auth.  Returns JSON with all active offers and full descriptions.
    Apply:      Offer pages have clean HTML forms — no CAPTCHA.
    """
    name = "recruitee"

    async def _fetch_board(self, client: httpx.AsyncClient, slug: str) -> list[JobData]:
        r = await client.get(f"https://{slug}.recruitee.com/api/offers/")
        r.raise_for_status()
        data = r.json()
        company = slug.replace("-", " ").title()
        out: list[JobData] = []
        for j in data.get("offers", []):
            offer_id = str(j.get("id", ""))
            slug_path = j.get("slug", "")
            loc = j.get("location", "")
            career_url = j.get("careers_url") or j.get("url", f"https://{slug}.recruitee.com/o/{slug_path}")
            sal = j.get("salary") or {}
            salary_str = ""
            if sal.get("min") or sal.get("max"):
                currency = sal.get("currency", "")
                salary_str = f"{sal.get('min', '')}-{sal.get('max', '')} {currency}".strip()
            out.append(JobData(
                source=self.name,
                external_id=f"{slug}:{offer_id}",
                company=j.get("company_name", company),
                title=j.get("title", ""),
                url=career_url,
                apply_url=career_url,
                location=loc,
                remote=j.get("remote", False) or "remote" in loc.lower(),
                employment_type=j.get("employment_type_code", ""),
                description=strip_html(j.get("description", "")),
                salary=salary_str,
                posted_at=_iso_ts(j.get("published_at") or j.get("created_at")),
            ))
        return out

    async def apply(self, ctx: ApplyContext) -> ApplyResult:
        await self._open_and_read(ctx, ctx.job.apply_url or ctx.job.url)
        # Recruitee career pages may require clicking an Apply button first.
        if not await page_has_text(ctx, r"resume|cover letter|first name|full name", timeout=3):
            await click_button(ctx, "apply for this (job|position)", "apply now", "^apply$")
            await ctx.human.pause(1.5, 3)
        await dismiss_cookies(ctx)
        answers = await fill_form(ctx, "form, .application-form, [class*=application]")
        res = await self._submit_and_confirm(
            ctx,
            ["submit application", "submit your application", "^submit$", "apply",
             "send application", "send my application"],
            r"thanks? (you |so much )?for (applying|your application|your interest)|"
            r"application (has )?(successfully )?(been )?(received|submitted)|"
            r"we.ve received your application|successfully applied|you.re all set",
        )
        res.extra["answers"] = answers
        return res
