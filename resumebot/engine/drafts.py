"""Saved application materials: preview and submission share the same files."""
from __future__ import annotations

import asyncio
import hashlib
import shutil
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from .. import db
from ..config import DATA_DIR
from ..sources.base import Materials

_locks: dict[int, asyncio.Lock] = {}


def get(job_id: int) -> dict | None:
    return db.kv_get(f'draft:{job_id}')


def load(job_id: int, version: str | None = None):
    draft = get(job_id)
    if not draft or draft.get('status') != 'ready':
        raise ValueError('Generate a resume preview first.')
    if version is not None and version != draft['version']:
        raise ValueError('This preview is out of date. Open the current preview before applying.')
    for key, digest in draft['hashes'].items():
        path = Path(draft[key])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('Saved materials changed or are missing. Generate a new preview.')
    materials = Materials(Path(draft['resume_pdf']), Path(draft['resume_docx']),
                          draft['cover_letter'], Path(draft['cover_letter_pdf']) if draft['cover_letter_pdf'] else None)
    return materials, SimpleNamespace(report=SimpleNamespace(**draft['report']), dropped=[])


async def prepare(job, refresh: bool = False):
    async with _locks.setdefault(job.id, asyncio.Lock()):
        if not refresh and (get(job.id) or {}).get('status') == 'ready':
            return load(job.id)
        db.kv_set(f'draft:{job.id}', {'status': 'preparing'})
        try:
            from .pipeline import prepare as generate
            materials, tailored = await generate(job)
            version = uuid4().hex[:16]
            folder = DATA_DIR / 'drafts' / str(job.id) / version
            folder.mkdir(parents=True, exist_ok=True)
            draft = {'status': 'ready', 'version': version, 'cover_letter': materials.cover_letter,
                     'report': asdict(tailored.report), 'hashes': {}}
            for key in ('resume_pdf', 'resume_docx', 'cover_letter_pdf'):
                source = getattr(materials, key)
                draft[key] = ''
                if source:
                    dest = folder / source.name
                    shutil.copyfile(source, dest)
                    draft[key] = str(dest)
                    draft['hashes'][key] = hashlib.sha256(dest.read_bytes()).hexdigest()
            db.kv_set(f'draft:{job.id}', draft)
            return load(job.id)
        except Exception as error:
            db.kv_set(f'draft:{job.id}', {'status': 'failed', 'error': str(error)[:1000]})
            raise
