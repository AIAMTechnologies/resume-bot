"""Ingest resumes, project folders, GitHub repos, and Claude exports into the master list."""
from __future__ import annotations

import shutil
from pathlib import Path

from sqlmodel import select

from .. import db
from ..config import DATA_DIR
from ..llm import complete_json
from ..models import Document
from . import claude_export, github, local_repos, master
from .extract import file_text, sha256_file, unzip

EXTRACT_SYSTEM = """You extract resume facts into structured JSON for a candidate's master profile.
Rules:
- Only record facts explicitly present in the source. Never infer employers, dates, titles,
  metrics, or skills that are not stated or directly evidenced (e.g. a language used in code).
- Bullets are achievement statements in resume style (action verb first, concrete, include
  metrics only if the source states them). Max ~30 words each.
- For projects, describe what was built, the stack, and any evidence of scale/impact.
- Skills are short canonical names ("PostgreSQL", "React", "AWS Lambda")."""

ITEM_SCHEMA = """Return {"items": [ ... ]} where each item is:
{"kind": "summary|experience|project|education|certification|skill|achievement",
 "title": "role / project name / degree / skill-group name",
 "organization": "company / school / issuer (empty for personal projects)",
 "location": "", "start": "YYYY-MM or YYYY or ''", "end": "YYYY-MM | 'Present' | ''",
 "url": "", "bullets": ["..."], "skills": ["..."]}"""


async def extract_items(text: str, source_label: str, hint: str = "") -> list[dict]:
    prompt = f"""Source type: {source_label}
{hint}
{ITEM_SCHEMA}

SOURCE:
<<<
{text}
>>>"""
    result = await complete_json(prompt, system=EXTRACT_SYSTEM, max_tokens=8000)
    return result.get("items", []) if isinstance(result, dict) else result


def _register(kind: str, name: str, path: str = "", sha: str = "") -> Document | None:
    """Create a Document row; returns None if this exact content was already ingested."""
    with db.session() as s:
        if sha and s.exec(select(Document).where(Document.sha256 == sha,
                                                 Document.status == "ingested")).first():
            return None
        doc = Document(kind=kind, name=name, path=path, sha256=sha)
        s.add(doc)
        s.commit()
        s.refresh(doc)
        return doc


def _finish(doc: Document, added: int = 0, error: str = "") -> None:
    doc.status = "failed" if error else "ingested"
    doc.error = error[:1000]
    doc.items_added = added
    db.save(doc)
    db.log(f"Ingested {doc.kind} '{doc.name}': {added} new items" if not error
           else f"Ingest failed for '{doc.name}': {error[:200]}",
           level="error" if error else "success", kind="profile")


async def ingest_file(path: Path) -> int:
    """Resumes and documents (pdf/docx/txt/md). Zips are expanded and routed."""
    if path.suffix.lower() == ".zip":
        return await ingest_zip(path)
    doc = _register("resume" if "resume" in path.name.lower() or "cv" in path.name.lower() else "document",
                    path.name, str(path), sha256_file(path))
    if doc is None:
        return 0
    try:
        text = file_text(path)
        if not text.strip():
            raise ValueError("no extractable text (scanned PDF? export as text-based PDF or DOCX)")
        items = await extract_items(text, doc.kind)
        added = master.merge_items(items, path.name)
        _finish(doc, added)
        return added
    except Exception as e:  # noqa: BLE001
        _finish(doc, error=str(e))
        return 0


async def ingest_zip(path: Path) -> int:
    dest = DATA_DIR / "uploads" / f"{path.stem}_unzipped"
    if dest.exists():
        shutil.rmtree(dest)
    unzip(path, dest)
    if list(dest.rglob("conversations.json")):
        return await ingest_claude_export(dest)
    total = 0
    projects = local_repos.find_projects(dest)
    for p in projects:
        total += await ingest_project_dir(p)
    if not projects:
        for f in dest.rglob("*"):
            if f.is_file() and f.suffix.lower() in {".pdf", ".docx", ".md", ".txt"}:
                total += await ingest_file(f)
    return total


async def ingest_project_dir(path: Path) -> int:
    total = 0
    for proj in local_repos.find_projects(path):
        doc = _register("project_dir", proj.name, str(proj))
        if doc is None:
            continue
        try:
            items = await extract_items(local_repos.dossier(proj), "local project folder",
                                        "Produce one 'project' item (plus 'skill' items if useful).")
            added = master.merge_items(items, f"project:{proj.name}")
            _finish(doc, added)
            total += added
        except Exception as e:  # noqa: BLE001
            _finish(doc, error=str(e))
    return total


async def ingest_github(username: str | None = None) -> int:
    total = 0
    for repo in await github.list_repos(username):
        doc = _register("github_repo", repo["full_name"], repo.get("html_url", ""),
                        f"gh:{repo['full_name']}:{repo.get('pushed_at')}")
        if doc is None:
            continue
        try:
            items = await extract_items(await github.repo_dossier(repo), "GitHub repository",
                                        "Produce one 'project' item with the repo URL.")
            added = master.merge_items(items, f"github:{repo['full_name']}")
            _finish(doc, added)
            total += added
        except Exception as e:  # noqa: BLE001
            _finish(doc, error=str(e))
    return total


async def ingest_claude_export(export_dir: Path) -> int:
    doc = _register("claude_export", export_dir.name, str(export_dir))
    if doc is None:
        return 0
    try:
        added = 0
        for chunk in claude_export.batches(claude_export.digests(export_dir)):
            items = await extract_items(
                chunk, "Claude conversation/project digests",
                "These are the candidate's own chats while building things. Group related "
                "conversations into distinct 'project' items describing what the candidate built "
                "and the stack used. Ignore Q&A that isn't building something. Do not credit the "
                "candidate with work the assistant merely suggested unless they clearly used it.")
            added += master.merge_items(items, "claude_export")
        _finish(doc, added)
        return added
    except Exception as e:  # noqa: BLE001
        _finish(doc, error=str(e))
        return 0


async def ingest_inbox_folder() -> int:
    """Everything dropped into data/inbox/ (files, zips, project folders)."""
    inbox = DATA_DIR / "inbox"
    total = 0
    for entry in sorted(inbox.iterdir()):
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            if list(entry.rglob("conversations.json")):
                total += await ingest_claude_export(entry)
            else:
                total += await ingest_project_dir(entry)
        else:
            total += await ingest_file(entry)
    return total


INFER_TITLES_SYSTEM = "You are a technical recruiter. Be realistic about seniority based on the evidence."


async def infer_titles() -> list[str]:
    result = await complete_json(
        "Based on this candidate's master profile, list 6-10 job titles they are a strong, "
        "realistic fit for (mix of exact-title variants recruiters actually post). "
        'Return {"titles": [...], "seniority": "junior|mid|senior|staff", "rationale": "..."}\n\n'
        + master.render(with_ids=False),
        system=INFER_TITLES_SYSTEM,
    )
    titles = result.get("titles", [])
    db.kv_set("inferred_titles", titles)
    db.kv_set("inferred_seniority", result.get("seniority", ""))
    db.log(f"Inferred target titles: {', '.join(titles)}", kind="profile", level="success")
    return titles


def target_titles() -> list[str]:
    from ..config import settings
    return settings().targets.titles or db.kv_get("target_titles") or db.kv_get("inferred_titles", [])
