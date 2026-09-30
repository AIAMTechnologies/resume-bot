# Resume Bot

Local-first job application assistant with a FastAPI dashboard, resume tailoring,
application tracking, and optional Telegram and Gmail integrations.

## Features

- Build a master profile from resumes, project folders, GitHub repositories, and exports.
- Discover and score jobs from Greenhouse, Lever, Ashby, Indeed, and public LinkedIn pages.
- Generate tailored PDF and DOCX resumes with ATS checks.
- Queue uncertain answers for review and control application pacing per source.
- Track applications and inbox replies from a local dashboard.

LinkedIn Easy Apply is assisted: materials are prepared for manual submission.
Workday applications are routed to the manual queue.

## Setup

Requires Python 3.12+ and Google Chrome. The project is designed primarily for macOS.
Run from an editable source checkout so configuration templates remain available.

```sh
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
resumebot init
```

Edit `.env`, `config/answers.yaml`, and `config/settings.yaml`. Review all example
answers, especially work authorization and consent, before using the application.
Configure company boards in `config/companies.yaml`.

The default LLM backend uses the Claude Code CLI. Authenticate the CLI and configure
its path/token as needed, or select `LLM_BACKEND=anthropic_api` and provide an API key.
Set `LLM_MODEL` to a model available to your account. `LLM_FAST_MODEL` (default
`claude-haiku-4-5`) handles the high-volume, simple calls: job scoring, short form answers and
inbox sorting. Leave it blank to use `LLM_MODEL` everywhere. With the API backend, your profile is
sent as a cached block, so repeat calls within a few minutes bill it at the cached rate.

The CLI backend bills every call against your Claude plan, the same allowance you use in Claude
Code. To keep the bot from using up your plan, use `LLM_BACKEND=anthropic_api` (pay per call).

Start the dashboard without the application scheduler:

```sh
resumebot run --dashboard-only
```

Open <http://127.0.0.1:8765> and upload your resumes under Master profile.
Keep the dashboard bound to localhost: it has no authentication.

## Usage

```sh
resumebot --help
resumebot ingest /path/to/resume.pdf
resumebot login indeed
resumebot doctor
resumebot run
```

`resumebot run` starts the scheduler and can submit applications automatically
according to your settings. To inspect a single application without submitting:

```sh
resumebot apply JOB_ID --dry-run
```

Optional integrations: configure Telegram in `.env`; use `resumebot gmail-auth
/path/to/client.json` for Gmail OAuth, or configure Gmail IMAP credentials.
`doctor` checks configured services and makes an LLM request.

## Development

```sh
pytest -q
```

Tests use an isolated temporary database. The form-filling test launches local
Chrome. Runtime files, resumes, browser profiles, credentials, and personal
configuration are excluded from Git. `PLAN.md` records the original design and
may differ from the current implementation.
