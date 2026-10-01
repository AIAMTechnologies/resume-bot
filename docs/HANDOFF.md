# Resume Bot — Handoff (2026-10-01, ~6:25 pm Toronto)

Paste the prompt at the bottom into a new Claude Code chat opened in `/Users/ammaralam/Documents/Resume Bot`.

## Ground rules (from Ammar)
- **One Claude session at a time.** Several sessions restarted the bot and edited the same files at once
  (conflicting Greenhouse settings, Chrome closed mid-application). Don't start a second session on this repo.
- **Never under $100,000.** Floor = `targets.min_salary: 100000`; entry-level titles (Tier 1, L1, New Grad,
  Junior, Support Specialist…) are excluded even when no pay is posted.
- **LinkedIn: assist mode only** — the bot never signs in (ban risk). **Never apply to Cohere.**
- Targets: security roles **and** ML/AI engineering roles. Wants ~20/day, 140/week, high quality.
- Commit as you go; **don't push to GitHub** without asking. End commits with the Co-Authored-By line.
- Don't build anything that reads/enters Greenhouse's emailed security code — that's bot detection. Ammar enters it.

## Current state
- Bot is running **detached** (`nohup .venv/bin/resumebot run`, parent PID 1, log `data/logs/bot.log`).
  Dashboard: http://127.0.0.1:8765. Automation ON, global pause OFF.
- **Greenhouse is PAUSED** (dashboard source pause) — Ammar is away until ~1 am Oct 2 and can't enter the emailed
  codes. 8 Greenhouse jobs sit queued for him (Chime, GitLab, Abnormal, Affirm, Gemini ×2, Vercel, Betterment).
  When he's back: resume Greenhouse on the dashboard (or `POST /control/resume/greenhouse`) and tell him per 🔐 ping.
- Ashby + Lever keep running (no codes). Ammar gave permission to **answer pending application questions for him**
  (best effort from his profile/answers; never claim experience the profile doesn't show; list what was answered).
- Latest commit: see `git log -5`. Working tree clean (stray `resumebot.db-shm/-wal` in repo root are junk).
- Totals: 31 jobs applied (24 today, Oct 1). Review: ~77 borderline jobs (60 Greenhouse), 5 manual cards.
- **Sysdig (Lever) is held as `failed`**: it showed an hCaptcha puzzle after Submit. Re-queue it when Ammar is at the
  Mac — the bot now pings 🔐 and waits for him to solve it.
- Cohere is now really excluded (`targets.exclude_companies` in settings.yaml + board removed + apply-time check).

## How applying works now
- Portals: Greenhouse, Ashby, Lever each run **3 parallel lanes**, each application in its own Chrome tab
  (`OWN_TAB_SOURCES` in browser/session.py, `PORTAL_LANES` / `SINGLE_LANE_SOURCES` in engine/scheduler.py).
- **Greenhouse emails a security code on every application.** The bot fills the form, brings the tab to the
  front, pings Telegram ("🔐 …"), and waits 20 min (`HANDOFF_MINUTES` in sources/ats_boards.py) for Ammar to
  type the code and click Submit. If he's away, those time out and fail → re-queue them later
  (or set greenhouse `enabled: false` / lanes to 1 while he's away).
- **Don't close the bot's Chrome window** while it runs — it kills every in-progress form.
- Restarting the bot interrupts in-progress forms → re-queue with
  `update job set status='queued' where status='applying' or status_reason like 'browser closed%'`.
  Stop the bot by PID (`kill <pid>`), not `pkill -f "resumebot run"` (that matches the shell running it).
- `config/settings.yaml` / `companies.yaml` are cached → need a bot restart. `config/answers.yaml` reloads on save.

## Answers
- `config/answers.yaml` → `screening:` holds the personal screening answers (all filled in by Ammar:
  relatives/government/conflict/criminal/non-compete/FINRA = No; outside business = Yes (Q4GEMS);
  shifts/on-call = Yes ("can work anytime"); arbitration, NDA/confidentiality, third-party background
  screening, AI policy = Yes). Matching rules: `SCREENING_RULES` in engine/questions.py.
  Blank line = the bot asks (never the AI). "previously_employed_here: auto" = No unless a past employer.
- Past employers (master profile): Gore Mutual, Q4GEMS, EY, HMB, Joey Co. Deloitte was only the **auditor**.
- Dashboard **Jobs page**: "❓ Answer N questions" opens a job's pending questions inline.
- Address/postal code are filled in (`contact.address`, `contact.postal_code`).

## Known issues / next steps
1. When Ammar is back: resume Greenhouse, re-queue Sysdig, tell him each 🔐 ping (code or CAPTCHA).
2. Review queue: ~77 borderline jobs — ask Ammar to approve/skip, or lower `auto_apply_score` (70) if he agrees.
   Several are security roles that timed out on codes and were re-scored lower (Mercury, Twilio, Vercel, Roblox).
3. Off-target applications went out today (Lightspeed Legal Ops, Mercury Support Ops PM, Ramp Technical Consultant ×2,
   1Password GTM Analyst). Asked Ammar whether to tighten titles to security + ML/AI engineering only — unanswered.
4. Likely-under-$100k applications Ammar may withdraw: Huntress SOC Support Specialist, Abnormal L1 Technical
   Support Engineer, Okta TAM Analyst (New Grad), Tailscale Customer Support Engineer (Tier 1).
5. Still `failed` from AI-quota errors (not re-queued; neither is a security/ML title): Chime Full-Stack Engineer
   (#7154), MongoDB Software Engineer 3 (#7063).
6. Restart without killing forms: global pause on → wait until no job is `applying` → `kill <pid>` → start again.
   `pgrep -f "resumebot run"` also matches any shell whose command contains that text — check with `ps` first.
7. Tests: run from a scratch copy with the example config (in-repo runs flake with PermissionError under the sandbox).
8. Read `docs/APPLY_PLAYBOOK.md` first — per-portal quirks, questions that park jobs, run log. Keep it updated.

---

## Prompt for the new chat

> I'm continuing work on my Resume Bot in this folder. Read `docs/HANDOFF.md` and `docs/APPLY_PLAYBOOK.md`
> first, then check the bot's state (dashboard http://127.0.0.1:8765, `data/resumebot.db`, `data/logs/bot.log`,
> `git log -5`). The bot is already running detached — don't start a second copy; restart it only by PID and
> re-queue anything interrupted. You're the only Claude session on this repo. Goal: keep applying through the
> queue (security + ML/AI roles, never under $100k, LinkedIn assist-only, no Cohere), tell me each time a
> Greenhouse security code needs me, fix whatever makes applications fail, update the playbook, and commit
> (don't push). Start by giving me a short status: applied today, queue size, anything waiting on me.
