"""Dashboard request tracing and diagnostic views."""
from __future__ import annotations

import html
import re
import time
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException

from .. import db, diagnostics
from ..models import Diagnostic


class BrowserError(BaseModel):
    message: str = Field(max_length=4000)
    stack: str = Field(default='', max_length=12000)
    page: str = Field(default='', max_length=2000)
    kind: str = Field(default='javascript', max_length=40)


def install(app, templates):
    def failure(request, status, detail):
        request_id = diagnostics.request_context.get().get('request_id', '')
        if 'text/html' in request.headers.get('accept', ''):
            return HTMLResponse('<!doctype html><title>Dashboard error</title><main style="font-family:system-ui;padding:32px">'
                f'<h1>That action could not finish</h1><p>{html.escape(str(detail))}</p>'
                f'<p>Request: <code>{request_id}</code></p><p><a href="/errors?request_id={request_id}">View error details</a>'
                ' · <a href="/jobs">Back to jobs</a></p></main>', status_code=status)
        return JSONResponse({'detail': detail, 'request_id': request_id, 'error_log': f'/errors?request_id={request_id}'}, status_code=status)

    @app.middleware('http')
    async def trace(request: Request, call_next):
        request_id = uuid4().hex[:16]
        job_match = re.match(r'/jobs/(\d+)(?:/|$)', request.url.path)
        token = diagnostics.request_context.set({'request_id': request_id, 'method': request.method,
            'path': request.url.path, 'job_id': int(job_match[1]) if job_match else None})
        started = time.monotonic()
        try:
            if request.method not in ('GET', 'HEAD') and request.url.path != '/api/browser-errors':
                diagnostics.record('Action received', kind='request')
            try:
                response = await call_next(request)
            except Exception as error:
                diagnostics.record(f'{type(error).__name__}: {error}', level='error', kind='request', error=error)
                response = failure(request, 500, 'Unexpected server error. See the error log for details.')
            if request.method not in ('GET', 'HEAD') and request.url.path != '/api/browser-errors':
                diagnostics.record(f'HTTP {response.status_code} in {int((time.monotonic()-started)*1000)} ms',
                    level='error' if response.status_code >= 500 else 'warning' if response.status_code >= 400 else 'info', kind='request')
            response.headers['X-Request-ID'] = request_id
            return response
        finally:
            diagnostics.request_context.reset(token)

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        detail = diagnostics.redact(str(error.detail))
        if request.url.path != '/favicon.ico':
            diagnostics.record(f'HTTP {error.status_code}: {detail}',
                level='error' if error.status_code >= 500 else 'warning', kind='request')
        response = failure(request, error.status_code, detail)
        if error.headers:
            response.headers.update(error.headers)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # Validation errors can include submitted values; retain only field locations.
        fields = ', '.join('.'.join(map(str, item['loc'])) for item in error.errors())
        diagnostics.record(f'Invalid input fields: {fields}', level='warning', kind='validation')
        return failure(request, 422, f'Check these fields: {fields}')

    def entries(level, request_id, job_id, q):
        query = db.select(Diagnostic)
        if level == 'problems':
            query = query.where(Diagnostic.level.in_(['error', 'warning']))
        elif level in ('error', 'warning', 'info', 'success'):
            query = query.where(Diagnostic.level == level)
        if request_id:
            query = query.where(Diagnostic.request_id == request_id)
        if job_id is not None:
            query = query.where(Diagnostic.job_id == job_id)
        if q:
            query = query.where(Diagnostic.message.contains(q))
        with db.session() as session:
            return list(session.exec(query.order_by(Diagnostic.ts.desc()).limit(300)))

    @app.get('/errors', response_class=HTMLResponse)
    async def error_log(request: Request, level: str = 'problems', request_id: str = '', job_id: int | None = None, q: str = ''):
        return templates.TemplateResponse(request, 'errors.html', {
            'active': 'errors', 'pending_count': 0, 'automatic': db.kv_get('automatic_mode', False),
            'telegram_connected': db.kv_get('telegram_connected', False),
            'entries': entries(level, request_id, job_id, q), 'level': level,
            'request_id': request_id, 'job_id': job_id, 'q': q})

    @app.get('/errors/export')
    async def export(level: str = 'problems', request_id: str = '', job_id: int | None = None, q: str = ''):
        rows = entries(level, request_id, job_id, q)
        return Response('\n'.join(diagnostics.redact(row.model_dump_json()) for row in rows),
            media_type='application/x-ndjson', headers={'Content-Disposition': 'attachment; filename="dashboard-errors.jsonl"'})

    @app.post('/api/browser-errors', status_code=204)
    async def browser_error(data: BrowserError):
        # URLs can contain query-string secrets; store only the path.
        pathname = urlsplit(data.page).path
        diagnostics.record(f'{data.kind} on {pathname}: {data.message}\n{data.stack}', level='error', kind='browser')
        return Response(status_code=204)
