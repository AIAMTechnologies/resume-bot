"""Find a LinkedIn "apply on company site" job on the company's own ATS board.

Guesses the board slug from the company name, fetches Greenhouse/Lever/Ashby boards through
their public APIs (cached per company), and fuzzy-matches the title. A match means the bot can
apply on the company's own form with no LinkedIn involvement.
"""
from __future__ import annotations

import difflib
import re

import httpx

from .. import db
from ..profile.master import norm
from .base import JobData

_SUFFIXES = r"\b(inc|incorporated|ltd|limited|llc|corp|corporation|co|company|group|of companies|canada|technologies|technology|labs|hq)\b"


def slug_candidates(company: str) -> list[str]:
    base = re.sub(r"[^a-z0-9 ]+", "", company.lower())
    short = re.sub(r"\s+", " ", re.sub(_SUFFIXES, "", base)).strip()
    out = []
    for name in dict.fromkeys([base, short]):
        if not name:
            continue
        out += [name.replace(" ", ""), name.replace(" ", "-")]
        first = name.split()[0]
        if len(first) > 3:
            out.append(first)
    return list(dict.fromkeys(out))


async def _board(client: httpx.AsyncClient, source: str, slug: str) -> list[JobData]:
    from .ats_boards import Ashby, Greenhouse, Lever
    from .extra_boards import JazzHR, Recruitee, SmartRecruiters, Workable
    adapter = {"greenhouse": Greenhouse, "lever": Lever, "ashby": Ashby,
               "smartrecruiters": SmartRecruiters, "jazzhr": JazzHR,
               "workable": Workable, "recruitee": Recruitee}[source]()
    try:
        return await adapter._fetch_board(client, slug)
    except Exception:  # noqa: BLE001 — 404s are the normal "not this slug" answer
        return []


def _location_ok(a: str, b: str) -> bool:
    """Guard against a same-named board belonging to a different company."""
    wa, wb = set(norm(a).split()), set(norm(b).split())
    if not wa or not wb or {"remote", "anywhere"} & (wa | wb):
        return True
    return bool((wa & wb) - {"on", "ontario", "canada", "ca", "united", "states", "us"}) or wa == wb


def _title_match(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


async def locate(client: httpx.AsyncClient, job: JobData) -> JobData | None:
    cache_key = f"ats_board:{norm(job.company)}"
    cached = db.kv_get(cache_key)
    if cached == "none":
        return None
    boards: list[tuple[str, str]] = [tuple(cached)] if cached else [
        (src, slug) for slug in slug_candidates(job.company)
        for src in ("greenhouse", "ashby", "lever", "smartrecruiters", "workable", "recruitee", "jazzhr")]
    for source, slug in boards:
        jobs = await _board(client, source, slug)
        if not jobs:
            continue
        db.kv_set(cache_key, [source, slug])
        best = max(jobs, key=lambda j: _title_match(j.title, job.title))
        if _title_match(best.title, job.title) >= 0.85 and _location_ok(best.location, job.location):
            best.company = job.company
            return best
        return None  # right board, job not listed there (yet)
    db.kv_set(cache_key, "none")
    return None
