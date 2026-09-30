from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from resumebot import db, diagnostics
from resumebot.engine import actions, pipeline
from resumebot.models import Diagnostic, Job
from resumebot.web.app import app


@pytest.fixture
def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')


def test_redacts_secrets_and_saves_traceback(monkeypatch):
    monkeypatch.setattr(diagnostics, 'env', lambda: SimpleNamespace(model_dump=lambda: {'telegram_bot_token': 'secret-token-123'}))
    try:
        raise RuntimeError('secret-token-123 Bearer hidden-auth https://api.telegram.org/bot123:secret/sendMessage')
    except RuntimeError as error:
        db.log(str(error), level='error', kind='test')
    with db.session() as session:
        row = session.exec(db.select(Diagnostic).order_by(Diagnostic.ts.desc())).first()
    assert 'RuntimeError' in row.traceback
    for secret in ['secret-token-123', 'hidden-auth', '123:secret']:
        assert secret not in row.message and secret not in row.traceback
    assert '[REDACTED]' in row.message


async def test_invalid_action_links_to_log(client):
    async with client:
        response = await client.post('/jobs/999999/preview', headers={'Accept': 'text/html'})
        assert response.status_code == 404
        request_id = response.headers['X-Request-ID']
        assert f'/errors?request_id={request_id}' in response.text
        log = await client.get('/errors', params={'request_id': request_id, 'level': 'all'})
        assert log.status_code == 200
        assert 'Action received' in log.text and 'HTTP 404' in log.text
        assert '/jobs/999999/preview' in log.text
        export = await client.get('/errors/export', params={'request_id': request_id, 'level': 'all'})
        assert 'attachment' in export.headers['Content-Disposition']
        assert request_id in export.text


async def test_background_error_retains_originating_request(client, monkeypatch):
    job = db.save(Job(source='greenhouse', external_id=uuid4().hex, title='Logging test', company='Test', url='https://example.com'))
    async def fail(job):
        raise RuntimeError('Simulated background failure')
    monkeypatch.setattr(pipeline, 'prepare', fail)
    async with client:
        response = await client.post(f'/jobs/{job.id}/preview')
        task = actions._tasks.get(job.id)
        if task:
            await task
        rows = await client.get('/errors/export', params={'request_id': response.headers['X-Request-ID']})
    assert 'Simulated background failure' in rows.text
    assert 'RuntimeError' in rows.text and 'traceback' in rows.text


async def test_browser_error_and_validation_do_not_log_form_values(client):
    async with client:
        response = await client.post('/api/browser-errors', json={
            'message': 'Simulated JS exception', 'stack': 'at test.js:10', 'page': '/jobs?private=do-not-store'})
        assert response.status_code == 204
        response = await client.post('/profile/items/not-a-number/toggle', data={'password': 'do-not-store'})
        assert response.status_code == 422
        rows = await client.get('/errors/export', params={'level': 'all'})
        assert 'Simulated JS exception' in rows.text
        assert 'do-not-store' not in rows.text
        assert (await client.post('/api/browser-errors', json={'message': 'x' * 5000})).status_code == 422


async def test_unhandled_server_error_has_diagnostic_id(client):
    async def fail():
        raise RuntimeError('Simulated server crash')
    app.add_api_route('/test-diagnostic-crash', fail)
    route = app.routes[-1]
    try:
        async with client:
            response = await client.get('/test-diagnostic-crash')
            assert response.status_code == 500
            request_id = response.json()['request_id']
            log = await client.get('/errors/export', params={'request_id': request_id})
            assert 'Simulated server crash' in log.text
            assert 'RuntimeError' in log.text
    finally:
        app.routes.remove(route)
