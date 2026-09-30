# Resume Bot — Plan

Local-first autonomous job application agent. Runs on this Mac (real Chrome, real home IP),
monitored from a web dashboard and Telegram. Packaged so the ATS-portal workers can move to a
server later without a rewrite.

## Decisions (from planning Q&A)

| Topic | Decision |
|---|---|
| Host | This MacBook first. `caffeinate` keeps it awake while running. |
| Sources | All: Greenhouse, Lever, Ashby (public APIs), LinkedIn Easy Apply, Indeed, Workday. Per-source adapter + per-source pacing profile. |
| LLM | Pluggable. Default `claude -p` (Claude Code CLI, uses your plan's usage). Switch to Anthropic API with one setting. |
| Autonomy | Auto-submit strong matches; borderline matches and unknown questions go to a review queue (dashboard + Telegram buttons). |
| Alerts | Telegram bot (notifications + commands + approve/skip buttons). |
| Inbox | Dedicated Gmail, read via IMAP app password; classifies replies (rejection/interview/assessment/offer) and updates statuses. |
| Targets | Remote + Canada hybrid/on-site + US + contract. Titles inferred from master profile, editable. |
| Profile inputs | Resumes (PDF/DOCX), local project folders, GitHub repos, Claude data export, arbitrary docs/zips. |

## Hard rules

1. **Truthful only.** Tailored resumes and answers are built only from facts in the master profile.
   A verifier pass rejects any bullet that introduces skills/employers/metrics not in the source facts.
2. **No CAPTCHA solving.** Any CAPTCHA / checkpoint / "unusual activity" → stop that source,
   cooldown (48h for LinkedIn), Telegram alert, job moves to *manual* queue.
3. **No password automation.** You log in once by hand (`resumebot login linkedin`); the persistent
   Chrome profile keeps the session.
4. **One browser at a time.** A person has one pair of hands — the bot never runs parallel browser sessions.

## Human-like behaviour (anti-ban)

- Real Google Chrome (not bundled Chromium), headed, persistent profile per source, your home IP.
- Mouse: Bézier paths with jitter, variable speed, occasional overshoot-and-correct.
- Typing: log-normal per-key delays, bursts and pauses, rare typo + backspace in free-text fields.
- Reading: scroll through the job description at reading speed before applying.
- Decoys: sometimes view a job and leave without applying; idle scrolling of the feed.
- Pacing per source (config/settings.yaml): daily cap, min/max gap, active hours, weekday/weekend
  caps, session length + breaks. LinkedIn default: 12/day, 4–12 min gaps, 9:00–18:00, 3 on weekends.

## Architecture

```
            ┌──────────── resumebot run (single process, asyncio) ────────────┐
 Telegram ◄─┤ notify/telegram  (long-poll commands, inline approve/skip)        │
            │ web/ (FastAPI + HTMX dashboard @ http://localhost:8765)          │
            │ engine/scheduler ── per-source loop ── pacing gate ── browser lock│
            │   discover → dedupe/filter → LLM score → tailor+ATS → apply       │
            │ inbox/gmail (IMAP poll) → classify → update application status    │
            └──────────────┬───────────────────────────────┬───────────────────┘
                     SQLite (data/resumebot.db)       data/ (resumes, screenshots,
                                                        chrome profiles, uploads)
```

### Pipeline per job
1. **Discover** — API (Greenhouse/Lever/Ashby company boards from `config/companies.yaml`) or
   browser search (LinkedIn/Indeed) with the target titles/locations.
2. **Filter** — dedupe (same company+title across sources), exclusions, location, seniority.
3. **Score** — LLM match 0–100 vs. master profile with reasons. ≥ auto threshold → apply;
   between → review; below → skip (reason stored).
4. **Tailor** — pick relevant experience/projects from master list, rephrase bullets toward JD
   keywords (truthful), render ATS-safe DOCX + PDF (single column, standard headings, no tables/
   images, standard fonts, text-extractable).
5. **ATS check** — parse rendered file back to text, verify sections/contact info are extractable,
   compute keyword coverage vs. JD, lint formatting. Below threshold → one re-tailor pass.
6. **Apply** — source adapter drives the form. Questions answered from `answers.yaml` →
   learned answer memory → LLM (factual, confident only) → otherwise review queue.
7. **Record** — application row, screenshot of confirmation, events, Telegram notice.

### Dashboard
- Overview: totals (all time / today / week), per-source bars over time, funnel
  (discovered → matched → applied → responded → interview), response rate, source health
  (active/paused, today vs cap, next action ETA), live activity feed, pause/resume controls.
- Applications: filterable table; detail shows JD, resume used, ATS score, answers, screenshot, replies.
- Jobs: everything discovered with score + skip reason; "apply anyway" / "skip".
- Review queue: approve / skip / type an answer (also doable from Telegram).
- Profile: master list, uploads (drag & drop), ingest status, target titles.

## Phases

- **Phase 1 (this scaffold):** config, DB, LLM layer, profile ingestion + master list, tailoring,
  ATS render/check, Greenhouse/Lever/Ashby discovery + apply, pacing + human input engine,
  scheduler, dashboard, Telegram, Gmail tracker, LinkedIn/Indeed adapters (first pass), CLI.
- **Phase 2:** live tuning of LinkedIn/Indeed selectors against your real accounts (supervised
  runs), Workday flow (account per tenant → mostly manual queue first), cover letter styles.
- **Phase 3:** analytics (which resume variants get replies), optional server split for ATS workers.

## What you need to provide
1. Resumes + project folders/zips + Claude export (drop into dashboard → Profile, or `data/inbox/`).
2. `config/answers.yaml`: work authorization, salary expectations, notice period, links, EEO prefs.
3. Telegram bot token (from @BotFather) + your chat id.
4. Dedicated Gmail address + app password.
5. GitHub username (+ optional token for private repos).
6. One-time manual logins: `resumebot login linkedin`, `resumebot login indeed`.
