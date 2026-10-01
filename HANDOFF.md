# Handoff: automation review & hardening (branch `claude/fervent-dijkstra-jfys6y`)

Paste the **Prompt for the next session** section into Claude Code on your Mac (inside this repo,
branch checked out) to continue. Everything below it is context.

## Prompt for the next session

> You are continuing a code-review-and-hardening pass on resume-bot (local-first job application
> bot: FastAPI dashboard, Playwright/Chrome form filling, Claude CLI/API for scoring and tailoring,
> SQLite via SQLModel). Work on branch `claude/fervent-dijkstra-jfys6y`; read `HANDOFF.md` first.
> Goals from the owner: (1) don't get flagged as a bot but stay fast, (2) keep learning from the
> applications the bot submits, (3) watch the error log, turn recurring errors into fixes.
>
> Already done and committed (see HANDOFF.md "Done"): challenge-detector false-positive fix,
> column migrations, retry policy with double-submit protection, JSON retry, batched AI answers,
> concurrent screening/discovery, browser-like HTTP headers, AI-answer memory, score-band insights,
> and a new error monitor (`resumebot/engine/health.py`) wired into the scheduler, `/errors` page,
> `resumebot errors` CLI and Telegram `/errors`.
>
> Do, in order:
> 1. `source .venv/bin/activate && pytest -q` — fix the one known failing test
>    (`tests/test_speed.py::test_prefetch_batches_unknown_questions_by_model`: the test needs the
>    same `patch_answers` fixture `tests/test_questions.py` uses, so `questions.answers` sees the
>    conftest persona; it is a test-fixture issue, not a product bug).
> 2. Write `tests/test_health.py` for `engine/health.py`: `fingerprint()`, `classify()`,
>    `scan()` creating/incrementing `ErrorPattern` rows, each remedy firing at its threshold
>    (dead board → `board_skipped`, repeated apply failures for one company → `company_manual`,
>    two challenges on a source → `pacing_scale` > 1 and `Gate.cfg` gaps scaled), unknown pattern
>    alerting once at 3 occurrences, `resolve()` reopening on recurrence, and `summary_line()`.
>    Also test `stats.score_bands()` and that `/errors` renders the "Recurring problems" card and
>    `POST /errors/patterns/{id}/resolve` works (see `tests/test_diagnostics.py` for the client
>    fixture).
> 3. Run the browser scenarios: `RESUMEBOT_E2E=1 pytest -q tests/test_e2e_scenarios.py`
>    (on the Mac with Google Chrome installed; in a container set
>    `RESUMEBOT_TEST_CHROMIUM=/path/to/chromium`). Known stale expectation to update:
>    `test_s1` asserts `question_hear == "Job board"` but commit c4d7f25 intentionally answers
>    "Company careers page" for board-discovered jobs; change the assertion. Scenario s9 now ends in
>    REVIEW ("possibly submitted") instead of FAILED, which its assertion already allows.
> 4. Update README.md: "Dashboard error log" section → describe Recurring problems, the automatic
>    remedies and their expiry (boards 7 days, companies 30 days, pacing 7 days), `resumebot errors`,
>    Telegram `/errors`; mention `matching.screen_concurrency`, the retry policy (pre-submit retry
>    once after 10 min; post-submit unknown state → Review + Telegram "Check this one"), and that
>    AI answers to closed questions are remembered after a successful submit (your review answers
>    always win). Note `config/settings.example.yaml` already has `screen_concurrency`.
> 5. Run the full suite again, then commit and `git push -u origin claude/fervent-dijkstra-jfys6y`.
>
> Optional follow-ups if time allows (not started): a "Reset remedies" action on `/errors`;
> a `health.scan()` call from `resumebot doctor`; per-field timeout learning (labels that time out
> repeatedly); outreach-note generation only when the LinkedIn card is opened (saves an AI call).

## Done in this session (all on the branch, unit suite 179/180 green)

| Area | Change | Files |
|---|---|---|
| Bot-detection | Challenge text patterns ("unusual activity", "security verification"…) now only count in the page title/headings/alerts or on short pages, so SOC job descriptions no longer pause a source for hours | `resumebot/browser/guards.py`, `tests/test_guards.py` |
| Bot-detection | One browser-like header set (UA mirrors installed Chrome version, client hints, fetch metadata) for LinkedIn guest pages and board APIs; `ControlOrMeta+A` instead of `Meta+A`; occasional "reading the question" pause between fields | `resumebot/sources/http.py`, `linkedin.py`, `ats_boards.py`, `browser/human.py`, `sources/forms.py` |
| Speed | One AI call per form for all questions the rules can't answer (`Answerer.prefetch`, `from_llm_batch`; fast model for short answers, main model for essays; falls back per-question on failure) | `engine/questions.py`, `sources/forms.py` (`_wanted`), `tests/test_speed.py` |
| Speed | Screening scores up to `matching.screen_concurrency` (3) jobs at once; board discovery fetches 4 boards at once and skips boards the error monitor disabled | `engine/pipeline.py` (`triage_new`), `sources/ats_boards.py`, `config.py`, `config/settings.example.yaml` |
| Robustness | `complete_json` re-asks once on malformed JSON | `llm/__init__.py` |
| Robustness | Scoring failures stop after 3 tries (job → Review with reason); tailoring failures retry once; apply failures before the submit click retry once after 10 min; after the click: validation error → FAILED, unknown state → REVIEW "possibly submitted" + Telegram "Check this one" with the screenshot link | `engine/pipeline.py` (`_failure_status`, `_exception_status`, `MAX_*`), `sources/base.py` (`ApplyContext.submit_clicked`), `ats_boards.py`, `indeed.py`, `tests/test_retry_policy.py` |
| Schema | Forward-only column migration (`db.add_missing_columns`), `Job.score_attempts`, `Job.apply_attempts`, `LearnedAnswer.origin`, new `ErrorPattern` table | `db.py`, `models.py`, `tests/test_migrations.py` |
| Learning | After a successful submit, AI answers to closed (option) questions are remembered (`origin="ai"`); your own answers are never overwritten by the AI | `engine/questions.py` (`remember_ai_answers`), `pipeline.py` |
| Learning | Overview card "What the match score predicts": replies/interviews by score band plus a threshold suggestion | `engine/stats.py` (`score_bands`), `web/app.py`, `web/templates/overview.html` |
| Error monitor | `engine/health.py`: fingerprints error-log rows into `ErrorPattern`s; remedies with thresholds: dead board slug ×2 → skipped 7 days; same company apply failure ×2 → that company prepared for you (30 days); challenge ×2 on a source → gaps ×1.5 for 7 days (via `Gate.cfg`); needs-you patterns (CLI missing, logged out, Gmail, Telegram); unknown pattern ×3 → one Telegram alert. Runs every 5 min (`health_loop`, always on), on `/errors` load, `resumebot errors`, Telegram `/errors`, daily summary line | `engine/health.py`, `engine/scheduler.py`, `engine/pacing.py`, `web/errors.py`, `web/templates/errors.html`, `__main__.py`, `notify/telegram.py` |
| Tests | Chrome-only form-filler tests now fall back to bundled Chromium (`RESUMEBOT_TEST_CHROMIUM`) | `tests/test_form_filler.py`, `tests/test_guards.py` |

## Review findings not (yet) acted on

- Dashboard has no auth; keep it on 127.0.0.1 (README already says so).
- `questions.from_memory` loads every learned answer per field; fine today, index/cached lookup if memory grows past a few thousand rows.
- `Indeed.apply` fills with `fill_form(ctx, "main, form, body")`; a prefetch happens per step, which is right, but Indeed pages are the most likely place for the generic button-choice scanner to mis-group; watch `Error log` for "Yes/No question".
- The error log lives only on the Mac (`data/` is git-ignored), so a cloud Claude session cannot watch it. Continuous monitoring is done in-app by `health_loop`; for a deeper pass, run `resumebot errors` or export `/errors/export` and paste it into a local Claude Code session.

## Session 2 (steps 1–5 above done)

- Fixed the order-dependent `test_speed` failure (fixture + clearing answers other tests taught the bot).
- `tests/test_health.py` (17 tests). Writing them found two remedy bugs, now fixed: the dead-board
  remedy always failed (discovery logs didn't name the source; they now say `<source> board '<slug>' …`),
  and the challenge remedy slowed down the challenge kind ("captcha") instead of the source.
- Browser scenarios 9/9 green on the Mac; `test_s1` expects "Company careers page".
- README: recurring problems and remedies, retries, learning from submissions.
- The branch is behind `main` by 5 commits (43168fe…d4dbe1a); merge before opening a PR.
