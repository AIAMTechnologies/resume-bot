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
Set `LLM_MODEL` to a model available to your account.

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

## Preview and apply

Run `resumebot run --dashboard-only` for **manual mode**: the dashboard and Telegram
are active, while automatic discovery and applications remain off. On Jobs, select
**Preview & apply**, then **Generate resume preview**. The preview includes the
saved PDF, DOCX, cover letter, and ATS report. **Apply now with this resume** uses
those same saved files; **Test form without submitting** leaves the job for review.
LinkedIn, Workday, and external sites remain manual submissions.

On Telegram, use `/jobs`, `/preview JOB_ID`, or `/apply JOB_ID`. The bot sends the
saved PDF and a confirmation button. Resume generation happens in the background,
so status and other commands remain responsive. Existing review buttons also lead
to previews. Queueing a job only schedules it when automatic mode is running.
The `/dashboard` localhost link works on the Mac; on your phone use the Telegram
commands directly.

## Codex plan fallback

Set `LLM_FALLBACK=codex_cli` to fall back from Claude to the installed Codex CLI.
Run `codex login` using ChatGPT first. `CODEX_CLI_PATH` and `CODEX_MODEL` are optional;
blank model uses the CLI default. Requires a recent CLI with `exec --ignore-user-config`
and the app-server account/usage methods.

The fallback forces ChatGPT authentication, removes API-key environment overrides,
and checks plan allowance before each request. Because the CLI has no per-request
paid-credit opt-out, the fallback conservatively refuses to run if a paid-credit
balance exists, allowance is unknown, or either usage window is at least 95% used.
It does not purchase credits or redeem reset credits. Usage is shared with your
other Codex sessions. Claude is retried after a 15-minute cooldown.
