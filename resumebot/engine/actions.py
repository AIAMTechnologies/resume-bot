"""Explicit user actions shared by dashboard and Telegram."""
from __future__ import annotations

import asyncio

from .. import db
from ..models import Job, JobStatus
from . import drafts, pipeline

_tasks: dict[int, asyncio.Task] = {}


def busy(job_id: int) -> bool:
    return job_id in _tasks and not _tasks[job_id].done()


def start(job_id: int, action: str, version: str = '', notify: bool = False) -> str:
    if action not in ('preview', 'refresh', 'apply', 'dry-run'):
        raise ValueError('Unknown job action.')
    with db.session() as s:
        job = s.get(Job, job_id)
    if not job:
        raise ValueError('Job not found.')
    if busy(job_id) or job.status == JobStatus.APPLYING:
        raise ValueError('This job already has an operation in progress.')
    if action in ('apply', 'dry-run', 'refresh') and job.status == JobStatus.APPLIED:
        raise ValueError('This job has already been submitted.')
    if action in ('apply', 'dry-run'):
        if not version:
            raise ValueError('Preview the saved resume before applying.')
        drafts.load(job_id, version)
    # Remove from automatic queue while a person reviews/prepares materials.
    if action in ('preview', 'refresh') and job.status == JobStatus.QUEUED:
        job.status, job.status_reason = JobStatus.REVIEW, 'resume preview requested'
        db.save(job)

    async def run():
        from ..notify import telegram
        try:
            if action in ('preview', 'refresh'):
                await drafts.prepare(job, refresh=action == 'refresh')
                if notify:
                    await telegram.send_preview(job_id)
                message = f'Resume preview ready for #{job_id}.'
            else:
                await pipeline.apply_job(job, dry_run=action == 'dry-run', draft_version=version)
                with db.session() as s:
                    current = s.get(Job, job_id)
                message = f'#{job_id}: {current.status} — {current.status_reason}'
                if notify:
                    await telegram.send(telegram.esc(message))
            db.log(message, kind='control', job_id=job_id)
        except Exception as error:
            message = f'{action} failed for #{job_id}: {error}'
            db.log(message, kind='control', level='error', job_id=job_id)
            if notify:
                await telegram.send(telegram.esc(message))
        finally:
            _tasks.pop(job_id, None)
    _tasks[job_id] = asyncio.create_task(run(), name=f'{action}:{job_id}')
    return f'{action.title()} started for #{job_id}.'
