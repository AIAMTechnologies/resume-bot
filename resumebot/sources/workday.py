"""Workday (and unknown company sites): prepared for you, applied by you.

Workday needs a separate account per company and has long multi-page forms, so phase 1 does
everything except the clicking: tailored resume + cover letter are ready, and the job lands in
the manual queue with a Telegram ping.
"""
from __future__ import annotations

from .base import ApplyContext, ApplyResult, JobData, ManualRequired, Source


class Workday(Source):
    name = "workday"

    async def discover(self, titles: list[str]) -> list[JobData]:
        return []  # Workday jobs arrive via rerouting from LinkedIn/Indeed

    async def apply(self, ctx: ApplyContext) -> ApplyResult:
        raise ManualRequired("Workday requires a per-company account")


class External(Workday):
    name = "external"
