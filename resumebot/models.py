"""Database models. SQLite via SQLModel."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    """All stored timestamps are timezone-aware UTC."""
    return datetime.now(timezone.utc)


class JobStatus:
    NEW = "new"                # discovered, not scored
    SKIPPED = "skipped"        # filtered or low score
    REVIEW = "review"          # waiting for human decision
    QUEUED = "queued"          # approved, waiting for a pacing slot
    APPLYING = "applying"
    APPLIED = "applied"
    FAILED = "failed"
    MANUAL = "manual"          # human must apply (captcha, workday, unsupported form)


class Job(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    source: str = Field(index=True)
    external_id: str
    company: str = Field(index=True)
    title: str
    location: str = ""
    remote: bool = False
    employment_type: str = ""
    url: str
    apply_url: str = ""
    description: str = ""
    salary: str = ""
    posted_at: Optional[datetime] = None
    discovered_at: datetime = Field(default_factory=utcnow, index=True)
    dedupe_key: str = Field(default="", index=True)

    match_score: Optional[int] = None
    match_reasons: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    missing_skills: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    status: str = Field(default=JobStatus.NEW, index=True)
    status_reason: str = ""
    easy_apply: bool = False
    updated_at: datetime = Field(default_factory=utcnow)


class AppStatus:
    SUBMITTED = "submitted"
    FAILED = "failed"
    # response states (from inbox tracker or manual)
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    ASSESSMENT = "assessment"
    INTERVIEW = "interview"
    OFFER = "offer"
    GHOSTED = "ghosted"


class Application(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id", index=True)
    source: str = Field(index=True)
    company: str = Field(index=True)
    title: str
    started_at: datetime = Field(default_factory=utcnow)
    submitted_at: Optional[datetime] = Field(default=None, index=True)
    duration_seconds: Optional[float] = None
    status: str = Field(default=AppStatus.SUBMITTED, index=True)
    error: str = ""
    resume_docx: str = ""
    resume_pdf: str = ""
    cover_letter: str = ""
    ats_score: Optional[int] = None
    keyword_coverage: Optional[float] = None
    answers: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    screenshot: str = ""
    last_response_at: Optional[datetime] = None
    response_summary: str = ""


class Event(SQLModel, table=True):
    """Activity log shown in the dashboard feed."""

    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=utcnow, index=True)
    level: str = "info"  # info | success | warning | error
    source: str = ""
    kind: str = ""       # discover | score | tailor | apply | challenge | inbox | system
    message: str = ""
    job_id: Optional[int] = None


class ReviewItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=utcnow, index=True)
    job_id: Optional[int] = Field(default=None, foreign_key="job.id")
    kind: str = "job"     # job (approve/skip) | question (needs an answer) | manual (human applies)
    question: str = ""
    proposed_answer: str = ""
    context: str = ""
    status: str = Field(default="pending", index=True)  # pending | approved | skipped | answered | done
    answer: str = ""
    resolved_at: Optional[datetime] = None
    telegram_message_id: Optional[int] = None


class LearnedAnswer(SQLModel, table=True):
    """Answers you approved once; reused for similar questions."""

    id: Optional[int] = Field(default=None, primary_key=True)
    question_norm: str = Field(index=True, unique=True)
    question: str
    answer: str
    uses: int = 0
    created_at: datetime = Field(default_factory=utcnow)


class SourceState(SQLModel, table=True):
    source: str = Field(primary_key=True)
    paused: bool = False
    paused_until: Optional[datetime] = None
    pause_reason: str = ""
    last_action_at: Optional[datetime] = None
    next_action_at: Optional[datetime] = None
    session_started_at: Optional[datetime] = None
    session_ends_at: Optional[datetime] = None
    last_discover_at: Optional[datetime] = None
    challenge_count: int = 0


class ProfileItem(SQLModel, table=True):
    """One entry in the master list."""

    id: Optional[int] = Field(default=None, primary_key=True)
    kind: str = Field(index=True)  # experience | project | education | certification | skill | achievement | summary
    title: str = ""
    organization: str = ""
    location: str = ""
    start: str = ""
    end: str = ""
    url: str = ""
    bullets: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    skills: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    sources: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    fingerprint: str = Field(default="", index=True)
    hidden: bool = False
    updated_at: datetime = Field(default_factory=utcnow)


class Document(SQLModel, table=True):
    """Anything fed into the master list: resume, project folder, repo, export."""

    id: Optional[int] = Field(default=None, primary_key=True)
    kind: str  # resume | project_dir | github_repo | claude_export | document
    name: str
    path: str = ""
    sha256: str = Field(default="", index=True)
    status: str = "pending"  # pending | ingested | failed
    error: str = ""
    items_added: int = 0
    created_at: datetime = Field(default_factory=utcnow)


class KV(SQLModel, table=True):
    """Small key/value store (inferred titles, telegram offset, flags)."""

    key: str = Field(primary_key=True)
    value: Any = Field(default=None, sa_column=Column(JSON))
