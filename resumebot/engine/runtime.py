"""In-process worker liveness and progress; never infer liveness from a saved flag."""
from __future__ import annotations

import asyncio
from ..models import utcnow

TASKS: dict[str, asyncio.Task] = {}
STATES: dict[str, dict] = {}


def register(tasks):
    TASKS.clear()
    STATES.clear()
    for task in tasks:
        TASKS[task.get_name()] = task


def update(name, **values):
    STATES.setdefault(name, {}).update(values, updated_at=utcnow())


def snapshot():
    rows = []
    for name, task in TASKS.items():
        row = {'name': name, 'phase': 'running', 'message': 'Starting', **STATES.get(name, {})}
        if task.done():
            if task.cancelled():
                row.update(phase='stopped', message='Worker was cancelled')
            elif task.exception():
                row.update(phase='failed', message=f'Worker stopped: {type(task.exception()).__name__}')
            elif row['phase'] != 'disabled':
                row.update(phase='stopped', message='Worker exited')
        elif name == 'telegram':
            from .. import db
            row.update(phase='waiting' if db.kv_get('telegram_connected') else 'error',
                       message='Listening for commands' if db.kv_get('telegram_connected') else 'Reconnecting')
        rows.append(row)
    return rows
