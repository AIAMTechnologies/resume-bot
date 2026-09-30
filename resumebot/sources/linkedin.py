"""LinkedIn — account-safe by design.

The bot NEVER signs in to LinkedIn or drives your logged-in LinkedIn session. That is the only
activity that can get an account restricted, so it simply doesn't happen here.

- Discovery: LinkedIn's public, logged-out job pages (plain HTTP, slow and low volume). Your
  account is not involved; the worst case is a temporary rate limit on your IP, which pauses
  LinkedIn discovery for a day.
- "Apply on company site" jobs: located on the company's Greenhouse/Lever/Ashby board and applied
  to there automatically — LinkedIn never sees it.
- Easy Apply jobs: "assist" mode. The bot tailors the resume, drafts a cover letter and an
  outreach note, and sends it all to Telegram. You tap Easy Apply and submit yourself.
"""
from __future__ import annotations

import asyncio
import random
import re
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

from .. import db
from ..config import settings
from .base import ApplyContext, ApplyResult, JobData, ManualRequired, Source

SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/144.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-CA,en;q=0.9"}


class RateLimited(Exception):
    pass


def parse_search(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for card in soup.select("[data-entity-urn^='urn:li:jobPosting:']"):
        job_id = card["data-entity-urn"].rsplit(":", 1)[-1]
        title = card.select_one(".base-search-card__title")
        company = card.select_one(".base-search-card__subtitle")
        location = card.select_one(".job-search-card__location")
        posted = card.select_one("time")
        out.append({
            "id": job_id,
            "title": title.get_text(strip=True) if title else "",
            "company": company.get_text(strip=True) if company else "",
            "location": location.get_text(strip=True) if location else "",
            "posted": posted.get("datetime") if posted else None,
        })
    return out


def parse_detail(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    desc = soup.select_one(".show-more-less-html__markup, .description__text")
    criteria = {}
    for li in soup.select("li.description__job-criteria-item"):
        h, v = li.select_one("h3"), li.select_one("span")
        if h and v:
            criteria[h.get_text(strip=True).lower()] = v.get_text(strip=True)
    apply_url = soup.select_one("code#applyUrl")
    offsite = "apply-link-offsite" in html or "offsite-apply" in html
    return {
        "description": desc.get_text("\n", strip=True) if desc else "",
        "employment_type": criteria.get("employment type", ""),
        "seniority": criteria.get("seniority level", ""),
        "offsite": offsite,
        "easy_apply": not offsite and ("apply-link-onsite" in html or "easy apply" in html.lower()),
        "apply_url": re.sub(r"^<!--|-->$", "", apply_url.get_text(strip=True)).strip('"') if apply_url else "",
    }


class LinkedIn(Source):
    name = "linkedin"
    max_details_per_run = 25

    async def _get(self, client: httpx.AsyncClient, url: str, **params) -> str:
        r = await client.get(url, params=params or None)
        if r.status_code in (429, 999) or (r.status_code == 403 and "authwall" in r.text):
            raise RateLimited(f"LinkedIn returned {r.status_code}")
        r.raise_for_status()
        return r.text

    def _searches(self, titles: list[str]) -> list[tuple[str, str, dict]]:
        loc = settings().targets.locations
        places: list[tuple[str, dict]] = []
        if loc.remote:
            places.append(("Canada", {"f_WT": "2"}))
        places += [(c.replace(", ON", ", Ontario, Canada"), {}) for c in loc.cities[:1]]
        picks = random.sample(titles, k=min(3, len(titles))) if titles else ["security analyst"]
        combos = [(t, p, e) for t in picks for p, e in places]
        random.shuffle(combos)
        return combos[:4]

    async def discover(self, titles: list[str]) -> list[JobData]:
        from .ats_locator import locate

        out: list[JobData] = []
        seen: set[str] = set()
        async with httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True) as client:
            try:
                cards: list[dict] = []
                for title, place, extra in self._searches(titles):
                    html = await self._get(client, SEARCH, keywords=title, location=place, f_TPR="r604800",
                                           start=0, **extra)
                    cards += [c for c in parse_search(html) if c["id"] not in seen and not seen.add(c["id"])]
                    await asyncio.sleep(random.uniform(4, 10))
                with db.session() as s:
                    from sqlmodel import select
                    from ..models import Job
                    known = set(s.exec(select(Job.external_id).where(Job.source == self.name)))
                fresh = [c for c in cards if c["id"] not in known][: self.max_details_per_run]
                for c in fresh:
                    d = parse_detail(await self._get(client, DETAIL.format(id=c["id"])))
                    posted = None
                    if c["posted"]:
                        posted = datetime.fromisoformat(c["posted"]).replace(tzinfo=timezone.utc)
                    jd = JobData(
                        source=self.name, external_id=c["id"], company=c["company"], title=c["title"],
                        url=f"https://www.linkedin.com/jobs/view/{c['id']}/", location=c["location"],
                        remote="remote" in c["location"].lower(), employment_type=d["employment_type"],
                        description=d["description"], posted_at=posted, easy_apply=d["easy_apply"],
                    )
                    if d["offsite"]:
                        # Company-site job: find it on the company's own ATS and apply there instead.
                        ats_job = await locate(client, jd)
                        if ats_job:
                            out.append(ats_job)
                            db.log(f"LinkedIn job found on {ats_job.source}: {jd.title} @ {jd.company}",
                                   source=self.name, kind="discover")
                        jd.apply_url = ats_job.url if ats_job else ""
                    out.append(jd)
                    await asyncio.sleep(random.uniform(3, 8))
            except RateLimited as e:
                from ..engine.pacing import Gate
                until = Gate(self.name).cooldown(f"public job pages rate-limited ({e})", hours=24)
                db.log(f"LinkedIn public pages rate-limited; pausing discovery until {until:%a %H:%M} UTC. "
                       "Your account is not affected.", level="warning", source=self.name, kind="discover")
        return out

    async def apply(self, ctx: ApplyContext) -> ApplyResult:
        raise ManualRequired("LinkedIn assist: tap Easy Apply yourself — materials attached")
