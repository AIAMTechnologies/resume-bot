from __future__ import annotations

import html
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ..browser.human import Human
    from ..engine.questions import Field
    from ..models import Job


@dataclass
class JobData:
    source: str
    external_id: str
    company: str
    title: str
    url: str
    apply_url: str = ""
    location: str = ""
    remote: bool = False
    employment_type: str = ""
    description: str = ""
    salary: str = ""
    posted_at: datetime | None = None
    easy_apply: bool = False


@dataclass
class Materials:
    resume_pdf: Path
    resume_docx: Path
    cover_letter: str = ""
    cover_letter_pdf: Path | None = None
    # Async callable that writes the cover letter on demand (only if the form asks for one).
    cover_factory: object = None


@dataclass
class ApplyContext:
    job: "Job"
    page: "Page"
    human: "Human"
    materials: Materials
    answer: Callable[["Field"], Awaitable[str]]
    dry_run: bool = False


@dataclass
class ApplyResult:
    submitted: bool
    note: str = ""
    screenshot: str = ""
    extra: dict = field(default_factory=dict)


class NeedsInput(Exception):
    """One or more required questions couldn't be answered; carries all of them."""

    def __init__(self, questions: list[tuple[str, str, list[str]]]):
        super().__init__("; ".join(q for q, _, _ in questions))
        self.questions = questions  # (question, proposed_answer, options)


class ManualRequired(Exception):
    """This application must be done by a human (e.g. needs an account)."""


class PostingClosed(Exception):
    """The posting was taken down (the link lands on the company's job list or a "not found" page)."""


class Source(ABC):
    name: str = ""
    uses_browser_for_discovery: bool = False

    @abstractmethod
    async def discover(self, titles: list[str]) -> list[JobData]:
        ...

    @abstractmethod
    async def apply(self, ctx: ApplyContext) -> ApplyResult:
        ...

    async def browse_only(self, page: "Page", human: "Human", job: "Job") -> None:
        """Decoy: open a job, read a bit, leave. Default: nothing."""


def strip_html(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<li[^>]*>", "• ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(s)).strip()
