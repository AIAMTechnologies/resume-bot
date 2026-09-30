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
from ..engine import actions, drafts, outcomes, pipeline, review, stats
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
# Action buttons are available in every template, including HTMX partials.
templates.env.globals["job_outcomes"] = outcomes.JOB_OUTCOMES
templates.env.globals["quick_outcomes"] = outcomes.QUICK_JOB_OUTCOMES
templates.env.globals["app_outcomes"] = outcomes.APP_OUTCOMES

from .errors import install as install_diagnostics
install_diagnostics(app, templates)

_bg: set[asyncio.Task] = set()


def background(coro) -> None:
    async def guarded():
        try:
            await coro
        except Exception as error:
            db.log(f"Dashboard operation failed: {error}", level="error", kind="control")
    t = asyncio.create_task(guarded())
    _bg.add(t)
    t.add_done_callback(_bg.discard)


def page(request: Request, name: str, **ctx):
    ctx.setdefault("global_pause", outcomes.global_paused())
    ctx.setdefault("flash", request.query_params.get("msg", ""))
    ctx.setdefault("flash_error", request.query_params.get("err", ""))
    ctx.setdefault("automatic", getattr(app.state, "automatic", False))
    ctx.setdefault("telegram_connected", db.kv_get("telegram_connected", False))
    ctx.setdefault("active", name.split(".")[0])
    ctx["pending_count"] = stats.overview()["pending_reviews"]
    return templates.TemplateResponse(request, name, ctx)


def back(request: Request, fallback: str = "/", msg: str = "", err: str = "") -> RedirectResponse:
    """Return to the page the button was on, with a one-line confirmation or error."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    parts = urlsplit(request.headers.get("referer") or fallback)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in ("msg", "err")]
    if msg:
        query.append(("msg", msg))
    if err:
        query.append(("err", err))
    url = urlunsplit(("", "", parts.path or "/", urlencode(query), parts.fragment))
    return RedirectResponse(url, status_code=303)


@app.get("/api/automation")
async def automation_status():
    return stats.automation()


@app.get("/partials/automation", response_class=HTMLResponse)
async def automation_partial(request: Request):
    return templates.TemplateResponse(request, "_automation.html", {"automation": stats.automation()})


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
    return page(request, "applications.html", apps=apps, jobs=jobs, automation=stats.automation(), f={"source": source, "status": status, "q": q})


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
    try:
        return back(request, msg=outcomes.record_application(app_id, status))
    except (outcomes.NotFound, outcomes.BadRequest) as error:
        raise HTTPException(error.status_code, str(error))
    except outcomes.OutcomeError as error:
        return back(request, err=str(error))


@app.get("/jobs", response_class=HTMLResponse)
async def jobs(request: Request, status: str = "", source: str = ""):
    return page(request, "jobs.html", jobs=stats.jobs(status, source, 400), f={"status": status, "source": source},
                statuses=[v for k, v in vars(JobStatus).items() if not k.startswith("_")])


@app.post("/jobs/{job_id}/{action}")
async def job_action(request: Request, job_id: int, action: str, version: str = Form("")):
    with db.session() as s:
        job = s.get(Job, job_id)
        if not job:
            raise HTTPException(404)
    if action in ("queue", "skip"):
        try:
            return back(request, "/jobs", msg=outcomes.record_job(job_id, "queue" if action == "queue" else "not_interested"))
        except outcomes.OutcomeError as error:
            return back(request, "/jobs", err=str(error))
    elif action in ("preview", "refresh", "apply-now", "dry-run"):
        try:
            actions.start(job_id, "apply" if action == "apply-now" else action, version)
        except ValueError as error:
            raise HTTPException(409, str(error))
        return RedirectResponse(f"/jobs/{job_id}/preview", status_code=303)
    else:
        raise HTTPException(400, "Unknown action")
    return back(request, "/jobs")


@app.post("/jobs/{job_id}/outcome/{key}")
async def job_outcome(request: Request, job_id: int, key: str):
    try:
        return back(request, "/jobs", msg=outcomes.record_job(job_id, key))
    except (outcomes.NotFound, outcomes.BadRequest) as error:
        raise HTTPException(error.status_code, str(error))
    except outcomes.OutcomeError as error:
        return back(request, "/jobs", err=str(error))


@app.post("/control/global-pause")
async def global_pause(request: Request, paused: str = Form(...)):
    return back(request, msg=outcomes.set_global_pause(paused == "on"))


@app.get("/jobs/{job_id}/preview", response_class=HTMLResponse)
async def resume_preview(request: Request, job_id: int):
    with db.session() as s:
        job = s.get(Job, job_id)
        events = list(s.exec(db.select(db.models.Event).where(db.models.Event.job_id == job_id)
                            .order_by(db.models.Event.ts.desc()).limit(8)))
    if not job:
        raise HTTPException(404)
    return page(request, "preview.html", job=job, draft=drafts.get(job_id),
                busy=actions.busy(job_id), events=events, active="jobs")


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
        if not it:
            raise HTTPException(404)
        it.hidden = not it.hidden
        s.add(it)
        s.commit()
    return back(request, "/profile")


@app.post("/control/{action}/{source}")
async def control(request: Request, action: str, source: str):
    if action not in ("pause", "resume", "discover") or (source != "all" and source not in SOURCES):
        raise HTTPException(400, "Unknown control or source")
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
        "LLM backend": e.llm_backend + (f" → {e.llm_fallback} fallback" if e.llm_fallback else "") + (f" ({find_claude_cli() or 'CLI NOT FOUND'})" if e.llm_backend == "claude_cli" else ""),
        "Model": e.llm_model,
        "Telegram": "connected" if db.kv_get("telegram_connected") else "offline / not configured",
        "Gmail tracker": e.gmail_address or "not configured",
        "GitHub": e.github_username or "not configured",
    }
    return page(request, "settings.html", files=files, checks=checks)


@app.get("/file")
async def file(path: str):
    p = Path(path).resolve()
    if not p.is_relative_to(DATA_DIR.resolve()) or not p.is_file():
        raise HTTPException(404)
    return FileResponse(p)


@app.get("/api/jobs/{job_id}")
async def job_json(job_id: int):
    with db.session() as s:
        j = s.get(Job, job_id)
    return JSONResponse(json.loads(j.model_dump_json()) if j else {})
