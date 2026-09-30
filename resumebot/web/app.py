"""Dashboard: FastAPI + Jinja + HTMX. Served on localhost only by default."""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import db
from ..config import CONFIG_DIR, DATA_DIR, env, settings
from ..engine import pipeline, review, stats
from ..engine.pacing import Gate, to_local
from ..models import Application, Job, JobStatus, ProfileItem
from ..profile import ingest, master
from ..sources import SOURCES

HERE = Path(__file__).parent
app = FastAPI(title="Resume Bot", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["local"] = lambda dt, fmt="%b %d %H:%M": to_local(dt).strftime(fmt) if dt else ""
templates.env.globals["refresh"] = lambda: settings().dashboard.refresh_seconds
templates.env.globals["sources"] = lambda: list(settings().pacing.sources)

_bg: set[asyncio.Task] = set()


def background(coro) -> None:
    t = asyncio.create_task(coro)
    _bg.add(t)
    t.add_done_callback(_bg.discard)


def page(request: Request, name: str, **ctx):
    ctx.setdefault("active", name.split(".")[0])
    ctx["pending_count"] = stats.overview()["pending_reviews"]
    return templates.TemplateResponse(request, name, ctx)


def back(request: Request, fallback: str = "/") -> RedirectResponse:
    return RedirectResponse(request.headers.get("referer") or fallback, status_code=303)


# ---------------- pages ----------------

@app.get("/", response_class=HTMLResponse)
async def overview(request: Request):
    return page(request, "overview.html", s=stats.overview(), health=stats.source_health(),
                by_source=stats.by_source(), skips=stats.skip_reasons(), funnel=stats.funnel(),
                recent=stats.recent_applications(8))


@app.get("/partials/feed", response_class=HTMLResponse)
async def feed(request: Request):
    return templates.TemplateResponse(request, "_feed.html", {"events": stats.recent_events(40)})


@app.get("/partials/health", response_class=HTMLResponse)
async def health(request: Request):
    return templates.TemplateResponse(request, "_health.html", {"health": stats.source_health()})


@app.get("/partials/kpis", response_class=HTMLResponse)
async def kpis(request: Request):
    return templates.TemplateResponse(request, "_kpis.html", {"s": stats.overview()})


@app.get("/api/charts")
async def charts(days: int = 30):
    return JSONResponse({"daily": stats.daily_series(days), "funnel": stats.funnel()})


@app.get("/applications", response_class=HTMLResponse)
async def applications(request: Request, source: str = "", status: str = "", q: str = ""):
    apps = stats.recent_applications(500, source=source, status=status, q=q)
    with db.session() as s:
        jobs = {j.id: j for j in s.exec(db.select(Job).where(Job.id.in_([a.job_id for a in apps])))}
    return page(request, "applications.html", apps=apps, jobs=jobs, f={"source": source, "status": status, "q": q})


@app.get("/applications/{app_id}", response_class=HTMLResponse)
async def application_detail(request: Request, app_id: int):
    with db.session() as s:
        a = s.get(Application, app_id)
        if not a:
            raise HTTPException(404)
        job = s.get(Job, a.job_id)
        events = list(s.exec(db.select(db.models.Event).where(db.models.Event.job_id == a.job_id)
                             .order_by(db.models.Event.ts)))
    return page(request, "application.html", a=a, job=job, events=events, active="applications")


@app.post("/applications/{app_id}/status")
async def set_app_status(request: Request, app_id: int, status: str = Form(...)):
    with db.session() as s:
        a = s.get(Application, app_id)
        a.status = status
        s.add(a)
        s.commit()
    db.log(f"Marked {a.company} as {status}", kind="control", job_id=a.job_id)
    return back(request)


@app.get("/jobs", response_class=HTMLResponse)
async def jobs(request: Request, status: str = "", source: str = ""):
    return page(request, "jobs.html", jobs=stats.jobs(status, source, 400), f={"status": status, "source": source},
                statuses=[v for k, v in vars(JobStatus).items() if not k.startswith("_")])


@app.post("/jobs/{job_id}/{action}")
async def job_action(request: Request, job_id: int, action: str):
    with db.session() as s:
        job = s.get(Job, job_id)
        if not job:
            raise HTTPException(404)
    if action == "queue":
        job.status, job.status_reason = JobStatus.QUEUED, "queued by you"
        db.save(job)
    elif action == "skip":
        job.status, job.status_reason = JobStatus.SKIPPED, "skipped by you"
        db.save(job)
    elif action in ("apply-now", "dry-run"):
        background(pipeline.apply_job(job, dry_run=action == "dry-run"))
        db.log(f"{'Dry run' if action == 'dry-run' else 'Apply now'} started for #{job.id}", kind="control", job_id=job.id)
    return back(request, "/jobs")


@app.get("/review", response_class=HTMLResponse)
async def review_page(request: Request):
    items = stats.pending_reviews()
    with db.session() as s:
        jobs = {j.id: j for j in s.exec(db.select(Job).where(Job.id.in_([i.job_id for i in items if i.job_id])))}
    return page(request, "review.html", items=items, jobs=jobs)


@app.post("/review/{item_id}")
async def review_action(request: Request, item_id: int, action: str = Form(...), answer: str = Form("")):
    review.resolve(item_id, action, answer)
    return back(request, "/review")


@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    with db.session() as s:
        docs = list(s.exec(db.select(db.models.Document).order_by(db.models.Document.created_at.desc()).limit(100)))
    items = master.all_items(include_hidden=True)
    return page(request, "profile.html", items=items, docs=docs, skills=master.all_skills(),
                titles=ingest.target_titles(), inferred=db.kv_get("inferred_titles", []),
                seniority=db.kv_get("inferred_seniority", ""), github=env().github_username)


@app.post("/profile/upload")
async def upload(request: Request, files: list[UploadFile] = File(...)):
    saved = []
    for f in files:
        name = Path(f.filename or "upload").name
        dest = DATA_DIR / "uploads" / name
        with open(dest, "wb") as out:
            shutil.copyfileobj(f.file, out)
        saved.append(dest)

    async def run():
        for p in saved:
            await ingest.ingest_file(p)
    background(run())
    db.log(f"Uploaded {len(saved)} file(s); ingesting in background", kind="profile")
    return back(request, "/profile")


@app.post("/profile/folder")
async def ingest_folder(request: Request, path: str = Form(...)):
    p = Path(path).expanduser()
    if not p.is_dir():
        db.log(f"Not a folder: {p}", level="error", kind="profile")
    else:
        background(ingest.ingest_project_dir(p))
        db.log(f"Scanning projects in {p}", kind="profile")
    return back(request, "/profile")


@app.post("/profile/github")
async def ingest_github(request: Request, username: str = Form("")):
    background(ingest.ingest_github(username or None))
    db.log(f"Importing GitHub repos for {username or env().github_username}", kind="profile")
    return back(request, "/profile")


@app.post("/profile/infer-titles")
async def infer_titles(request: Request):
    background(ingest.infer_titles())
    return back(request, "/profile")


@app.post("/profile/titles")
async def save_titles(request: Request, titles: str = Form("")):
    db.kv_set("target_titles", [t.strip() for t in titles.splitlines() if t.strip()])
    db.log("Target titles updated", kind="profile")
    return back(request, "/profile")


@app.post("/profile/items/{item_id}/toggle")
async def toggle_item(request: Request, item_id: int):
    with db.session() as s:
        it = s.get(ProfileItem, item_id)
        it.hidden = not it.hidden
        s.add(it)
        s.commit()
    return back(request, "/profile")


@app.post("/control/{action}/{source}")
async def control(request: Request, action: str, source: str):
    names = list(SOURCES) if source == "all" else [source]
    for n in names:
        if action in ("pause", "resume"):
            Gate.set_paused(n, action == "pause", "paused from dashboard")
        elif action == "discover":
            background(pipeline.discover(n))
    db.log(f"{action.title()} {source} from dashboard", kind="control")
    return back(request)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    from ..llm.claude_cli import find_claude_cli
    files = {}
    for name in ("settings.yaml", "answers.yaml", "companies.yaml"):
        p = CONFIG_DIR / name
        files[name] = p.read_text() if p.exists() else "(missing — run `resumebot init`)"
    e = env()
    checks = {
        "LLM backend": e.llm_backend + (f" ({find_claude_cli() or 'CLI NOT FOUND'})" if e.llm_backend == "claude_cli" else ""),
        "Model": e.llm_model,
        "Telegram": "connected" if e.telegram_bot_token and e.telegram_chat_id else "not configured",
        "Gmail tracker": e.gmail_address or "not configured",
        "GitHub": e.github_username or "not configured",
    }
    return page(request, "settings.html", files=files, checks=checks)


@app.get("/file")
async def file(path: str):
    p = Path(path).resolve()
    if not str(p).startswith(str(DATA_DIR.resolve())) or not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


@app.get("/api/jobs/{job_id}")
async def job_json(job_id: int):
    with db.session() as s:
        j = s.get(Job, job_id)
    return JSONResponse(json.loads(j.model_dump_json()) if j else {})
