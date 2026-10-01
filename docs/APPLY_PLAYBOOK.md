# Apply Playbook — how to get applications through fast

Living notes from real runs. Read this before debugging a stuck or slow application.
Update it whenever a run teaches something new (date each entry).

## Fast path (what a clean run looks like)
1. Job queued (score ≥ auto_apply_score, or approved by you) → prefetch has already tailored the resume.
2. Open the posting with `Human(brisk=True)` (portal sources: greenhouse / lever / ashby) — reading capped at 12 s.
3. `fill_form` pass 1: upload files (resume first; cover letter only if the form asks — `cover_factory`).
4. Pass 2: answer fields — memory (learned answers) → rules (`questions.RULES`) → LLM → NeedsHuman.
5. Re-scan up to 3 rounds for follow-up questions that appear after answers.
6. Submit, confirm the success page/email.

Biggest time sinks, in order: AI tailoring (~1–3 min) > unknown questions going to review > page load/timeouts.

## Before a big run (pre-flight, saves the most time)
- `automatic_mode` = true, `global_pause` = false (dashboard top bar).
- Queue has ≥ 2× the target count (approve review items or lower `auto_apply_score`).
- Answer every open review **question** first — one unanswered question parks a job.
- AI plan has allowance (`ai_wait_until` null). Claude ↔ Codex fail over automatically.
- Laptop awake and online (DNS errors in Telegram polling = network dropped / Mac slept).

## Per-portal notes

### Greenhouse (`job-boards.greenhouse.io`, `boards.greenhouse.io`)
- Hidden `requiredInput` mirrors exist next to real inputs — skip `aria-hidden` fields or you time out.
- Resume field may be labelled just "Attach" — detect it by `#resume` / name.
- The upload box is replaced after upload; mark it done tolerantly (don't re-find the old node).
- "Location (City)" is an autocomplete — type "Toronto", pick the option containing "Toronto, Ontario" (never Ohio).
- EEO block (gender, race, hispanic/latino, veteran, disability) — all answered by rules.
- Common custom questions already ruled: work authorization (CA yes / US no + TN), sponsorship,
  relocation, how did you hear, website (GitHub fallback), languages, consent, marketing opt-out.

### Ashby (`jobs.ashbyhq.com`)
- Two forms on the page: the "Autofill from resume" box and the real form — skip the autofill box.
- Yes/No questions are **button groups**, not radios — generic detection via `data-rb-group` (reset each scan).
- Follow-up questions appear after answering — the re-scan handles them.

- Checkbox groups: the required marker (`_required_*` class) is on the **question title label**, a sibling of
  the option checkbox inside the `fieldset` — `isReq` now checks `fieldset > label[class*=required]`.
  (Missed before → "Missing entry for required field: I hereby certify…" on every OpenAI form.)
- OpenAI (and Roblox on Greenhouse) require agreeing to an **Applicant Arbitration Agreement**. Legal waivers
  (arbitration / class action / jury / waive / non-compete / "bound by the terms") are never auto-checked —
  `LEGAL_WAIVER_RE` raises NeedsHuman until you answer once (then remembered).

- Clicking "Apply for this Job" can be swallowed by a **cookie banner** → bot stayed on the description page
  ("submit button not found", Plaid). Now: verify URL has `/application`, else go there directly.

### All portals
- **Cookie banners:** `dismiss_cookies()` clicks "Necessary only / Reject all / Decline" (never Accept All).
- **Confirmation wording varies** — Tailscale said "Thanks so much for applying… successfully been received" and was
  marked failed though submitted. Success regex widened. If a job shows "no confirmation; unknown state",
  open its screenshot (`application.screenshot`) before retrying — it may already be submitted.

### Lever (`jobs.lever.co`)
- Single-page form; "Additional information" textarea is optional — leave blank unless asked.

### LinkedIn
- **Assist mode only.** Never signs in (BLOCKED_HOSTS). Produces cards for you to apply manually.

### Workday / Taleo / iCIMS / SuccessFactors
- Need an account → ManualRequired (you apply). Not automated.

## Questions that park jobs (answer once → saved forever)
**Personal screening questions now live in `config/answers.yaml → screening:`** (reloaded on save, no restart).
Rules in `questions.SCREENING_RULES` map every wording to one line; a blank line = the bot asks you (never the AI).
Legal agreements (`agree_to_arbitration`, `third_party_background_screening`, `ai_policy_agreement`):
"Yes" = agree, "No" = job becomes a manual card. "previously_employed_here: auto" = No unless it's a past employer.
Jobs page: a "❓ Answer N questions" notice opens the answer form inline.

| Question pattern | Answer source |
|---|---|
| Street address / postal code | `answers.yaml: address` (still blank) |
| FINRA licenses | learned answer |
| Non-compete / post-employment restrictions | learned answer |
| Relatives at company | learned answer |
| Weekend / fixed shift availability | ask per job |
| Criminal record | ask you |
| Arbitration agreement (OpenAI, Roblox) | ask you |
| "What brought you to this posting" | HEARD_RE (fixed 2026-10-01) |
| "Current Location…" | `__city_region` rule (fixed 2026-10-01) |
| Gender options "Man/Woman" | closest_option maps male→Man (fixed 2026-10-01) |
| SMS / text-message updates consent | `__no_marketing` → No |
| "Where do you plan on working from (payroll tax)" | `__city_region` |
| Outside business activity (Okta) | Yes — Q4GEMS runs alongside Gore Mutual |
| Location preference list without Toronto | Remote |
| Ashby "right to work basis" (BeyondTrust) | location-dependent: Canada → permanent; don't save as memory |

## Greenhouse email security code
**Update 2026-10-01 evening:** even one apply per ~10 min got a code every time — Greenhouse now flags this
browser on every application, so slowing down is not enough. **Hand-off:** when the code screen appears the bot
brings the filled tab to the front, sends a Telegram ping, and waits 20 min (`HANDOFF_MINUTES`) for YOU to type
the emailed code and click Submit; it records the submission when the confirmation page appears.
Earlier: fast back-to-back Greenhouse applies trigger "A verification code was sent to … enter the 8-character code to
confirm you're a human". That is bot detection — the bot does NOT read/enter it. Mitigation: Greenhouse runs ONE
lane with 3–7 min gaps (SINGLE_LANE_SOURCES, settings greenhouse gaps). Jobs that hit it end "no confirmation;
unknown state" and the screenshot shows the Security code boxes.

## Speed levers
- **Parallel tabs (2026-10-01):** company portals (greenhouse/lever/ashby) get their own tab per application
  (`OWN_TAB_SOURCES` in browser/session.py) and run `PORTAL_LANES = 3` lanes each (scheduler.py).
  Before this, ONE shared tab + lock meant only one application at a time across all sources.
  LinkedIn/Indeed still share the single locked tab (account safety).
- In-flight (`applying`) jobs count toward the per-company 90-day cap, so lanes don't pile onto one company.
- Restarting the bot interrupts in-flight jobs → they land in Review "interrupted"; re-queue them
  (`update job set status='queued' where id in (...)`). Tailored drafts are cached, so a re-run is form time only.
- `prefetch_loop` tailors the next 2 queued portal jobs while the current one fills — keep it on.
- Two-stage scoring: fast model first, main model only if fast ≥ 55.
- `tailor_with_retry` does one AI call unless `ats.retailor_attempts` is set.
- Portal gaps 20–60 s; session breaks 2–5 min (settings.yaml `sources.*`).

## Run log
- 2026-09-30 night: 5 submitted (Huntress ×2, Datadog, Klaviyo, Perseus). Stalled because the queue
  emptied (most jobs scored 60–69 → review) and automation was switched off at 00:03.
- 2026-10-01: Added ML/AI targeting (21 target titles, 243 ML jobs re-screened); automation back on.
- 2026-10-01 10:36–11:20: Automation on; BeyondTrust (198s) + SentinelOne (144s) submitted. Fixed: parallel tabs,
  Ashby required checkboxes, legal-waiver guard, heard/location/gender rules. Avg form ≈ 2.5–3.5 min.
- 2026-10-01 11:20: +Tailscale (verified from screenshot), +Zscaler MDR Manager (301s). Fixed cookie banners,
  Ashby /application navigation, confirmation wording, SMS/payroll-location rules.
- 2026-10-01 17:40: Push for 50 in 3h. Added 3 Lever + 24 Ashby boards (security/AI; no defense/crypto exchanges, no Cohere)
  → 2,392 new jobs; screening ranks target titles first (~10 s per AI score). global_daily_cap 60→80 (needs bot restart:
  settings/companies are lru_cached). Greenhouse stays single-lane with the code hand-off — not tuned to evade the check.
- 2026-10-01 ~18:00: Greenhouse switched to 3 parallel lanes (SINGLE_LANE_SOURCES empty), gaps 30–60 s, cap 45 —
  you enter each emailed code in its tab. Pause Greenhouse when away or each tab stalls 20 min then fails.
