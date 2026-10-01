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

## Dashboard error log

Open **Error log** in the sidebar (`/errors`) to inspect errors and warnings, filter
by job or request ID, and download matching entries. Select **All activity** to see
an action arrive, its HTTP result, and related background activity. Failed requests
show a request ID and a link to their diagnostic details. Browser JavaScript,
resource-loading, and HTMX failures are captured too (up to 20 reports per page load).

Detailed tracebacks are available under **Technical details**. A rotating backup log
is written to `data/logs/diagnostics.jsonl` (5 MB per file, three backups), including
when diagnostic database writes fail. Request bodies, headers, and traceback locals
are not logged; configured credentials and common token patterns are redacted.
Logs stay local under the Git-ignored `data/` directory. Capture begins when this
version starts; older activity remains in the overview feed.

### Recurring problems and automatic remedies

The **Recurring problems** card at the top of the error log groups repeated errors into
patterns (every 5 minutes while the bot runs, and whenever you open the page). The bot fixes
the shapes it knows and lists the rest:

| Problem | Remedy | Lasts |
|---|---|---|
| A company board returns 404/410 twice (or fails 4 times) | Discovery skips that board; fix or remove the slug in `config/companies.yaml` | 7 days |
| Applications to one company fail twice the same way | That company's applications are prepared for you to submit instead | 30 days |
| A source hits a bot check twice | Gaps between actions on that source grow 1.5× (up to 3×); the challenge cooldown still applies | 7 days |
| Claude CLI missing, a source logged out, Gmail or Telegram errors | Marked **needs you** with what to do, and sent to Telegram once | until fixed |
| An error with no known remedy recurs 3 times | One Telegram alert; open the error log for the traceback | — |

Remedies expire on their own, so nothing needs resetting. **Resolved** closes a pattern;
it reopens if the error comes back. The same list is available as `resumebot errors`
(`--resolved` includes closed ones) and as `/errors` in Telegram, and the daily summary
mentions open problems.

### Retries

Scoring that fails 3 times parks the job in Review with the error (use **Score now** to retry).
Tailoring and form failures before the submit click are retried once after 10 minutes. After
the submit click, a validation error marks the job Failed; an unclear result moves it to Review
as "possibly submitted" and Telegram asks you to **Check this one** with the screenshot, so a
job is never submitted twice.

### Learning from submissions

After a successful submit, AI answers to closed (multiple-choice) questions are remembered and
reused on later forms. Answers you give during review always win: the AI never overwrites them.
`matching.screen_concurrency` (default 3, in `config/settings.yaml`) sets how many jobs the AI
scores at once; all of a form's unanswered questions go to the AI in one call.

## Live automation visibility

The Applications page now includes a live panel that refreshes every five seconds:
job counts at each stage, the exact role being screened, worker liveness, last and
next checks, source schedules, and the reason each queued job is waiting. Completed
applications appear separately in Application history. `/api/automation` exposes
the same status for diagnostics. Worker liveness comes from the running tasks,
not just a saved automated-mode flag.

Screening prioritizes titles in the configured `targets.title_keywords` order,
then discovery time. Existing match thresholds and review rules still apply.
Source working hours are set independently in local `config/settings.yaml`;
`[0, 24]` allows a source to run at any hour without changing its daily caps or gaps.
