"""Configuration: secrets from .env, behaviour from config/*.yaml."""
from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = Path(os.environ.get("RESUMEBOT_DATA_DIR", ROOT / "data"))


class Env(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    llm_backend: str = "claude_cli"
    claude_cli_path: str = ""
    claude_code_oauth_token: str = ""  # from `claude setup-token`; lets the bot use your plan headlessly
    anthropic_api_key: str = ""
    llm_model: str = "claude-sonnet-5-5"          # resumes, cover letters, written answers
    # Scores decide what gets submitted automatically, so they use the main model by default.
    score_with_fast_model: bool = False
    llm_fast_model: str = "claude-haiku-4-5"      # job scoring, short answers, inbox sorting; blank = llm_model
    llm_fallback: str = ""
    codex_cli_path: str = ""
    codex_model: str = ""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    gmail_address: str = ""
    gmail_app_password: str = ""

    github_username: str = ""
    github_token: str = ""

    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8765


class SourcePacing(BaseModel):
    enabled: bool = True
    mode: str = "auto"  # auto | manual
    daily_cap: int = 10
    weekend_cap: int = 3
    active_hours: tuple[int, int] = (9, 18)
    min_gap_seconds: int = 180
    max_gap_seconds: int = 600
    session_minutes: tuple[int, int] = (25, 50)
    break_minutes: tuple[int, int] = (20, 60)
    browse_only_ratio: float = 0.0
    challenge_cooldown_hours: int = 24
    discover_every_hours: int = 6


class Pacing(BaseModel):
    global_daily_cap: int = 60
    sources: dict[str, SourcePacing] = Field(default_factory=dict)


class Locations(BaseModel):
    remote: bool = True
    remote_regions: list[str] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    preferred_regions: list[str] = Field(default_factory=list)


class Targets(BaseModel):
    titles: list[str] = Field(default_factory=list)
    # Any title containing one of these goes to AI scoring even if it doesn't match a target title.
    title_keywords: list[str] = Field(default_factory=lambda: [
        "security", "cyber", "soc", "incident", "threat", "iam", "identity", "grc", "siem", "dfir",
        "infosec", "detection", "vulnerability", "risk", "automation", "csirt", "cirt", "forensic", "forensics",
        "malware", "penetration", "pentest", "red team", "blue team", "appsec", "devsecops", "privacy",
        "compliance", "governance", "trust"])
    locations: Locations = Field(default_factory=Locations)
    employment_types: list[str] = Field(default_factory=lambda: ["full_time"])
    exclude_title_keywords: list[str] = Field(default_factory=list)
    exclude_companies: list[str] = Field(default_factory=list)
    min_salary_cad: int | None = None   # legacy name
    min_salary: int | None = None       # skip jobs whose posted pay range tops out below this


class Matching(BaseModel):
    auto_apply_score: int = 75
    review_score: int = 60
    max_job_age_days: int = 21
    # Many companies cap or frown on repeat applications (Cohere: 5 per 90 days).
    max_apps_per_company_90d: int = 2


class ATS(BaseModel):
    min_keyword_coverage: float = 0.55
    retailor_attempts: int = 1
    include_cover_letter: str = "when_optional"


class Human(BaseModel):
    typo_rate: float = 0.02
    wpm: tuple[int, int] = (45, 75)
    reading_wpm: tuple[int, int] = (220, 320)


class Inbox(BaseModel):
    poll_minutes: int = 15


class Dashboard(BaseModel):
    refresh_seconds: int = 10


class Settings(BaseModel):
    timezone: str = "America/Toronto"
    targets: Targets = Field(default_factory=Targets)
    matching: Matching = Field(default_factory=Matching)
    ats: ATS = Field(default_factory=ATS)
    pacing: Pacing = Field(default_factory=Pacing)
    human: Human = Field(default_factory=Human)
    inbox: Inbox = Field(default_factory=Inbox)
    dashboard: Dashboard = Field(default_factory=Dashboard)


def _load_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        example = CONFIG_DIR / name.replace(".yaml", ".example.yaml")
        path = example if example.exists() else path
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


@lru_cache
def env() -> Env:
    return Env()


@lru_cache
def settings() -> Settings:
    return Settings.model_validate(_load_yaml("settings.yaml"))


@lru_cache
def answers() -> dict[str, Any]:
    return _load_yaml("answers.yaml")


@lru_cache
def companies() -> dict[str, list[str]]:
    return {k: list(v or []) for k, v in _load_yaml("companies.yaml").items()}


def reload() -> None:
    for fn in (env, settings, answers, companies):
        fn.cache_clear()


def ensure_dirs() -> None:
    for sub in ("uploads", "inbox", "resumes", "screenshots", "profiles", "logs"):
        (DATA_DIR / sub).mkdir(parents=True, exist_ok=True)


def init_config_files() -> list[str]:
    """Copy *.example.yaml / .env.example to their live names if missing."""
    created = []
    for example in CONFIG_DIR.glob("*.example.yaml"):
        live = example.with_name(example.name.replace(".example", ""))
        if not live.exists():
            shutil.copy(example, live)
            created.append(str(live.relative_to(ROOT)))
    env_file = ROOT / ".env"
    if not env_file.exists():
        shutil.copy(ROOT / ".env.example", env_file)
        created.append(".env")
    return created
