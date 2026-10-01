"""resumebot CLI."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(add_completion=False, help="Local-first autonomous job application bot.")
console = Console()


def _boot():
    from . import db
    db.init_db()


@app.command()
def init():
    """Create config files, data folders, and the database."""
    from .config import ensure_dirs, init_config_files
    ensure_dirs()
    created = init_config_files()
    _boot()
    for c in created:
        console.print(f"[green]created[/] {c}")
    console.print("""
[bold]Next steps[/]
 1. Fill in [cyan]config/answers.yaml[/] (contact, work authorization, salary, EEO prefs)
 2. Fill in [cyan].env[/] (Telegram token, Gmail app password, GitHub username)
 3. [cyan]resumebot telegram-chat-id[/] after messaging your bot once
 4. [cyan]resumebot import-chrome-profile ResumeBot[/] (close that Chrome profile first), then
    [cyan]resumebot login indeed[/] — log in by hand once (LinkedIn never needs a login)
 5. [cyan]resumebot doctor[/] to verify everything
 6. [cyan]resumebot run[/] and open http://127.0.0.1:8765 → Master profile → upload resumes""")


@app.command()
def run(dashboard_only: bool = typer.Option(False, "--dashboard-only", help="Dashboard + Telegram; only apply when explicitly requested"),
        keep_awake: bool = typer.Option(True, help="Keep the Mac awake while running (caffeinate)")):
    """Start the dashboard + Telegram + scheduler (discover, score, apply, track inbox)."""
    import uvicorn

    from .browser.session import browsers
    from .config import env
    from .engine import scheduler
    from .web.app import app as web

    _boot()
    caf = None
    if keep_awake and sys.platform == "darwin":
        caf = subprocess.Popen(["caffeinate", "-ims", "-w", str(os.getpid())])

    async def main():
        config = uvicorn.Config(web, host=env().dashboard_host, port=env().dashboard_port, log_level="warning")
        server = uvicorn.Server(config)
        await scheduler.start_background(automatic=not dashboard_only)
        console.print(f"[green]Dashboard:[/] http://{env().dashboard_host}:{env().dashboard_port}"
                      + ("  [yellow](automation off — turn it on from the dashboard or /automation on)[/]" if dashboard_only else ""))
        try:
            await server.serve()
        finally:
            await scheduler.shutdown()
            await browsers.shutdown()

    try:
        asyncio.run(main())
    finally:
        if caf:
            caf.terminate()


@app.command("import-chrome-profile")
def import_chrome_profile(profile: str = typer.Argument("ResumeBot", help="Chrome profile name, folder, or email")):
    """Seed the bot's browser from one of your Chrome profiles (close that profile's windows first)."""
    from .browser.session import import_chrome_profile as do_import
    dest = do_import(profile)
    console.print(f"[green]Imported[/] Chrome profile '{profile}' → {dest}")
    console.print("Next: [cyan]resumebot login indeed[/] (use 'Continue with Google' if you signed up that way). "
                  "LinkedIn needs no login — the bot never signs in to it.")


@app.command()
def login(source: str):
    """Open the source's Chrome profile so you can log in by hand (once)."""
    from .browser.session import interactive_login
    console.print(f"Log in to [bold]{source}[/] in the window that opens, then close the window.")
    asyncio.run(interactive_login(source))
    console.print("[green]Saved.[/] The bot will reuse this session.")


@app.command()
def ingest(paths: list[Path] = typer.Argument(None, help="Files, zips, or project folders"),
           github: bool = typer.Option(False, help="Import your GitHub repos"),
           inbox: bool = typer.Option(False, help="Ingest everything in data/inbox/"),
           titles: bool = typer.Option(True, help="Re-infer target titles afterwards")):
    """Build the master list from resumes, projects, GitHub, and Claude exports."""
    from .profile import ingest as ing
    _boot()

    async def main():
        total = 0
        for p in paths or []:
            p = p.expanduser()
            total += await (ing.ingest_project_dir(p) if p.is_dir() else ing.ingest_file(p))
        if github:
            total += await ing.ingest_github()
        if inbox:
            total += await ing.ingest_inbox_folder()
        console.print(f"[green]{total} new master-list items[/]")
        if titles and total:
            console.print("Target titles: " + ", ".join(await ing.infer_titles()))
    asyncio.run(main())


@app.command()
def discover(source: str):
    """Run discovery for one source now."""
    from .engine import pipeline
    _boot()
    console.print(f"{asyncio.run(pipeline.discover(source))} new jobs")


@app.command()
def triage(limit: int = 25):
    """Filter and score NEW jobs now."""
    from .engine import pipeline
    _boot()
    asyncio.run(pipeline.triage_new(limit))


@app.command()
def apply(job_id: int, dry_run: bool = typer.Option(False, "--dry-run", help="Fill everything, don't submit"),
          hold: int = typer.Option(60, help="Dry run: seconds to keep the browser open afterwards")):
    """Apply to one job now (ignores pacing). Use --dry-run for supervised testing."""
    from .browser.session import browsers
    from .engine import pipeline
    from .models import Job
    from . import db
    _boot()

    async def main():
        with db.session() as s:
            job = s.get(Job, job_id)
        if not job:
            console.print("[red]No such job[/]")
            return
        a = await pipeline.apply_job(job, dry_run=dry_run)
        console.print(a.model_dump() if a else "No application record (see dashboard activity).")
        if dry_run and hold:
            console.print(f"Dry run: leaving the browser open {hold}s for you to inspect…")
            await asyncio.sleep(hold)
        await browsers.shutdown()
    asyncio.run(main())


@app.command()
def status():
    """Print today's numbers and source health."""
    from .engine import stats
    _boot()
    s = stats.overview()
    console.print(f"Today {s['today']} · week {s['week']} · total {s['total']} · replies {s['responses']} "
                  f"({s['response_rate']}%) · interviews {s['interviews']} · review queue {s['pending_reviews']}")
    t = Table("source", "today/cap", "status", "next", "last discovery")
    for h in stats.source_health():
        t.add_row(h["name"], f"{h['today']}/{h['cap']}", ("PAUSED " + h["reason"]) if h["paused"] else h["status"],
                  h["next"], h["last_discover"])
    console.print(t)


@app.command("telegram-chat-id")
def telegram_chat_id():
    """After sending your bot any message, print the chat id to put in .env."""
    from .notify import telegram
    chats = asyncio.run(telegram.discover_chat_ids())
    if not chats:
        console.print("No messages found. Send your bot a message in Telegram first, then re-run.")
    for cid, who in chats:
        console.print(f"TELEGRAM_CHAT_ID={cid}   ({who})")


@app.command("inbox-backfill")
def inbox_backfill(days: int = typer.Option(30, help="How many days back to load")):
    """Load past replies into the Emails tab (and update application statuses)."""
    from .inbox import gmail
    _boot()
    console.print(f"[green]{asyncio.run(gmail.backfill(days))} email(s) added[/]")


@app.command("gmail-auth")
def gmail_auth(client_file: Path = typer.Argument(None, help="OAuth client JSON downloaded from Google Cloud")):
    """Connect the application Gmail with Google sign-in (read-only). Run once."""
    import shutil as _sh
    from .inbox import gmail_api
    if client_file:
        _sh.copy(Path(client_file).expanduser(), gmail_api.CLIENT_FILE)
    if not gmail_api.CLIENT_FILE.exists():
        console.print(f"[red]Missing {gmail_api.CLIENT_FILE}[/] — pass the downloaded client JSON path.")
        raise typer.Exit(1)
    console.print("A browser window will open. Choose your application Gmail and click Allow.")
    console.print(f"[green]Connected:[/] {gmail_api.authorize()}")


@app.command()
def doctor():
    """Check configuration and connections."""
    from .config import CONFIG_DIR, answers, env
    from .llm.claude_cli import find_claude_cli
    _boot()
    ok = lambda b: "[green]✓[/]" if b else "[red]✗[/]"  # noqa: E731
    e = env()
    console.print(f"{ok((CONFIG_DIR / 'settings.yaml').exists())} config/settings.yaml")
    contact = answers().get("contact", {})
    console.print(f"{ok(bool(contact.get('email') and contact.get('first_name')))} answers.yaml contact info")
    console.print(f"{ok(Path('/Applications/Google Chrome.app').exists())} Google Chrome installed")
    if e.llm_backend == "claude_cli":
        path = find_claude_cli()
        console.print(f"{ok(bool(path))} Claude Code CLI: {path or 'not found — set CLAUDE_CLI_PATH'}")
    else:
        console.print(f"{ok(bool(e.anthropic_api_key))} ANTHROPIC_API_KEY")
    try:
        from .llm import get_llm
        reply = asyncio.run(get_llm().complete("Reply with exactly: pong", max_tokens=10))
        console.print(f"{ok('pong' in reply.lower())} LLM round-trip ({reply.strip()[:40]})")
    except Exception as ex:  # noqa: BLE001
        console.print(f"{ok(False)} LLM round-trip: {ex}")
    if e.telegram_bot_token:
        from .notify import telegram
        try:
            me = asyncio.run(telegram.call("getMe"))
            console.print(f"{ok(True)} Telegram bot @{me['username']}"
                          + ("" if e.telegram_chat_id else " [yellow](TELEGRAM_CHAT_ID missing)[/]"))
        except Exception as ex:  # noqa: BLE001
            console.print(f"{ok(False)} Telegram: {ex}")
    else:
        console.print(f"{ok(False)} Telegram not configured")
    from .inbox import gmail_api
    if gmail_api.configured():
        try:
            console.print(f"{ok(True)} Gmail API {gmail_api.profile()['emailAddress']}")
        except Exception as ex:  # noqa: BLE001
            console.print(f"{ok(False)} Gmail API: {ex}")
    elif e.gmail_address and e.gmail_app_password:
        import imaplib
        try:
            m = imaplib.IMAP4_SSL("imap.gmail.com")
            m.login(e.gmail_address, e.gmail_app_password)
            m.logout()
            console.print(f"{ok(True)} Gmail IMAP {e.gmail_address}")
        except Exception as ex:  # noqa: BLE001
            console.print(f"{ok(False)} Gmail IMAP: {ex}")
    else:
        console.print(f"{ok(False)} Gmail tracker not configured — run: resumebot gmail-auth <client.json>")
    from .config import DATA_DIR
    prof = DATA_DIR / "profiles" / "main" / "Default"
    console.print(f"{ok(prof.exists())} bot browser profile"
                  f"{'' if prof.exists() else ' — run: resumebot import-chrome-profile ResumeBot'}")


if __name__ == "__main__":
    app()
