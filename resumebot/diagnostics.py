"""Local, redacted diagnostics. Never records request bodies, headers, or locals."""
from __future__ import annotations

import logging
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
import re
import traceback
from uuid import uuid4

from .config import DATA_DIR, env
from .models import Diagnostic

request_context: ContextVar[dict] = ContextVar('dashboard_request', default={})
_logger = None


def redact(value: str) -> str:
    text = str(value)
    for key, secret in env().model_dump().items():
        if any(part in key for part in ('token', 'password', 'api_key')) and secret:
            text = text.replace(str(secret), '[REDACTED]')
    text = re.sub(r'(?i)(bearer\s+)[^\s\"\'<>]+', r'\1[REDACTED]', text)
    text = re.sub(r'(https://api\.telegram\.org/bot)[^/\s]+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)((?:token|password|api[_-]?key|secret)=)[^\s&]+', r'\1[REDACTED]', text)
    return text


def file_logger():
    global _logger
    if _logger is None:
        folder = DATA_DIR / 'logs'
        folder.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger(f'resumebot.diagnostics.{DATA_DIR}')
        logger.setLevel(logging.INFO)
        logger.propagate = False
        handler = RotatingFileHandler(folder / 'diagnostics.jsonl', maxBytes=5_000_000, backupCount=3)
        handler.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(handler)
        _logger = logger
    return _logger


def record(message: str, *, level: str = 'info', kind: str = 'system',
           job_id: int | None = None, error: BaseException | None = None) -> str:
    from . import db
    ctx = request_context.get()
    entry = Diagnostic(id=uuid4().hex[:16], message=redact(message)[:8000], level=level, kind=kind,
        request_id=ctx.get('request_id', ''), method=ctx.get('method', ''), path=ctx.get('path', ''),
        job_id=job_id if job_id is not None else ctx.get('job_id'), traceback=redact(''.join(traceback.format_exception(error)))[:24000] if error else '')
    # File log remains available if the database is the source of the failure.
    try:
        file_logger().info(entry.model_dump_json())
    except Exception:
        logging.getLogger(__name__).error('Could not write diagnostics file')
    try:
        with db.session() as session:
            session.add(entry)
            session.commit()
    except Exception:
        logging.getLogger(__name__).error('Could not save diagnostic %s to database; check data/logs', entry.id)
    return entry.id
