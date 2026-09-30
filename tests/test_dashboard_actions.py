from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from resumebot import db
from resumebot.engine import actions, drafts, pipeline
from resumebot.models import Application, Job, JobStatus
from resumebot.notify import telegram
from resumebot.resume.ats import ATSReport
from resumebot.sources.base import ApplyResult, Materials
from resumebot.web.app import app


@pytest.fixture
def job():
    return db.save(Job(source='greenhouse', external_id=uuid4().hex, company='Test', title='Engineer',
                       url='https://example.com/job', status=JobStatus.QUEUED))


@pytest.fixture
def prepared(monkeypatch, tmp_path):
    pdf, docx = tmp_path / 'resume.pdf', tmp_path / 'resume.docx'
    pdf.write_bytes(b'%PDF-1.4 exact preview')
    docx.write_bytes(b'exact docx')
    async def generate(job):
        return Materials(pdf, docx, 'Cover letter'), SimpleNamespace(report=ATSReport(90, .8), dropped=[])
    monkeypatch.setattr(pipeline, 'prepare', generate)
    monkeypatch.setattr(telegram, 'enabled', lambda: False)
    return pdf


@pytest.fixture
def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')


async def test_dashboard_preview_and_exact_submission(client, job, prepared, monkeypatch):
    async with client:
        for route in ['/', '/jobs', '/review', '/applications', '/profile', '/settings']:
            assert (await client.get(route)).status_code == 200
        response = await client.post(f'/jobs/{job.id}/preview')
        assert response.status_code == 303
        if task := actions._tasks.get(job.id):
            await task
        draft = drafts.get(job.id)
        html = (await client.get(response.headers['location'])).text
        assert 'Exact resume for this application' in html
        assert 'Apply now with this resume' in html
        assert (await client.get('/file', params={'path': draft['resume_pdf']})).content == prepared.read_bytes()
        assert (await client.post(f'/jobs/{job.id}/apply-now')).status_code == 409
        assert (await client.post(f'/jobs/{job.id}/apply-now', data={'version': 'old'})).status_code == 409
        seen = []
        @asynccontextmanager
        async def page(source):
            yield SimpleNamespace(viewport_size={"width": 1280, "height": 800})
        async def screenshot(*args, **kwargs):
            return ''
        async def submit(ctx):
            seen.append(ctx.materials.resume_pdf.read_bytes())
            return ApplyResult(True, 'Submitted test fixture')
        monkeypatch.setattr(pipeline.browsers, 'page', page)
        monkeypatch.setattr(pipeline, '_screenshot', screenshot)
        monkeypatch.setattr(pipeline.SOURCES['greenhouse'], 'apply', submit)
        response = await client.post(f'/jobs/{job.id}/apply-now', data={'version': draft['version']})
        assert response.status_code == 303
        if task := actions._tasks.get(job.id):
            await task
        assert seen == [b'%PDF-1.4 exact preview']
        assert (await client.post(f'/jobs/{job.id}/apply-now', data={'version': draft['version']})).status_code == 409
        with db.session() as s:
            assert s.get(Job, job.id).status == JobStatus.APPLIED
            records = s.exec(db.select(Application).where(Application.job_id == job.id)).all()
            assert len(records) == 1 and records[0].resume_pdf == draft['resume_pdf']


async def test_dry_run_does_not_submit_or_requeue(job, prepared, monkeypatch):
    await drafts.prepare(job)
    @asynccontextmanager
    async def page(source):
        yield SimpleNamespace(viewport_size={"width": 1280, "height": 800})
    async def apply(ctx):
        assert ctx.dry_run
        return ApplyResult(False, 'Dry run finished')
    async def screenshot(*args, **kwargs):
        return ''
    monkeypatch.setattr(pipeline.browsers, 'page', page)
    monkeypatch.setattr(pipeline, '_screenshot', screenshot)
    monkeypatch.setattr(pipeline.SOURCES['greenhouse'], 'apply', apply)
    actions.start(job.id, 'dry-run', drafts.get(job.id)['version'])
    with pytest.raises(ValueError, match='in progress'):
        actions.start(job.id, 'apply', drafts.get(job.id)['version'])
    await actions._tasks[job.id]
    with db.session() as s:
        assert s.get(Job, job.id).status == JobStatus.REVIEW
        assert not s.exec(db.select(Application).where(Application.job_id == job.id)).all()


async def test_preview_failure_is_visible(client, job, monkeypatch):
    async def fail(job):
        raise RuntimeError('Plan limit reached')
    monkeypatch.setattr(pipeline, 'prepare', fail)
    actions.start(job.id, 'preview')
    await actions._tasks[job.id]
    async with client:
        response = await client.get(f'/jobs/{job.id}/preview')
    assert 'Plan limit reached' in response.text


async def test_changed_files_block_submission(job, prepared):
    await drafts.prepare(job)
    draft = drafts.get(job.id)
    from pathlib import Path
    Path(draft['resume_pdf']).write_bytes(b'changed')
    with pytest.raises(ValueError, match='changed'):
        actions.start(job.id, 'apply', draft['version'])


async def test_telegram_preview_confirmation(job, prepared, monkeypatch):
    sent = []
    async def document(path, caption):
        sent.append(('document', path))
        return 123
    async def send(text, buttons=None, **kwargs):
        sent.append(('message', buttons))
        return 124
    monkeypatch.setattr(telegram, 'send_document', document)
    monkeypatch.setattr(telegram, 'send', send)
    assert 'Preview started' in await telegram.handle_command(f'/preview {job.id}')
    await actions._tasks[job.id]
    assert sent[0][0] == 'document'
    button = next(v for k,v in sent if k == 'message')[0][0][1]
    assert button == f'apply:{job.id}:{drafts.get(job.id)["version"]}'
    assert 'Usage' in await telegram.handle_command('/apply')
    assert 'out of date' in await telegram.handle_callback(f'apply:{job.id}:old')
    db.kv_set('automatic_mode', False)
    assert 'Manual mode' in await telegram.handle_command('/run')


async def test_file_route_rejects_sibling_directory(client, tmp_path):
    from resumebot.config import DATA_DIR
    sibling = DATA_DIR.parent / (DATA_DIR.name + '-outside')
    sibling.mkdir(exist_ok=True)
    secret = sibling / 'private.txt'
    secret.write_text('private')
    async with client:
        assert (await client.get('/file', params={'path': str(secret)})).status_code == 404
        assert (await client.post('/control/discover/invalid')).status_code == 400


async def test_controls_answers_and_profile(client, job):
    from resumebot.models import ProfileItem, ReviewItem
    profile = db.save(ProfileItem(kind='skill', title='Python'))
    review = db.save(ReviewItem(job_id=job.id, kind='question', question='Test question ' + uuid4().hex))
    async with client:
        assert (await client.post('/control/pause/greenhouse')).status_code == 303
        assert db.source_state('greenhouse').paused
        assert (await client.post('/control/resume/greenhouse')).status_code == 303
        assert not db.source_state('greenhouse').paused
        assert (await client.post(f'/review/{review.id}', data={'action': 'answer', 'answer': 'Yes'})).status_code == 303
        with db.session() as s:
            assert s.get(ReviewItem, review.id).answer == 'Yes'
        assert (await client.post(f'/profile/items/{profile.id}/toggle')).status_code == 303
        with db.session() as s:
            assert s.get(ProfileItem, profile.id).hidden
        assert (await client.post('/profile/items/999999/toggle')).status_code == 404
        assert (await client.post('/applications/999999/status', data={'status': 'interview'})).status_code == 404
        assert (await client.post('/applications/999999/status', data={'status': 'garbage'})).status_code == 400


async def test_telegram_retries_registration_and_ignores_other_chats(monkeypatch):
    import asyncio
    seen, registered = [], []
    monkeypatch.setattr(telegram, 'env', lambda: SimpleNamespace(telegram_bot_token='fake', telegram_chat_id='123'))
    async def call(method, **kwargs):
        if method == 'setMyCommands':
            registered.append(True)
            if len(registered) == 1:
                raise RuntimeError('temporary outage')
            return True
        if method == 'getUpdates' and not seen:
            return [
                {'update_id': 8001, 'message': {'chat': {'id': 999}, 'text': '/jobs'}},
                {'update_id': 8002, 'message': {'chat': {'id': 123}, 'text': '/jobs', 'reply_to_message': {'message_id': 4}}},
            ]
        raise asyncio.CancelledError()
    async def command(text):
        seen.append(text)
        return 'OK'
    async def noop(*args, **kwargs):
        return None
    monkeypatch.setattr(telegram, 'call', call)
    monkeypatch.setattr(telegram, 'handle_command', command)
    monkeypatch.setattr(telegram, 'send', noop)
    monkeypatch.setattr(asyncio, 'sleep', noop)
    with pytest.raises(asyncio.CancelledError):
        await telegram.poll_forever()
    assert len(registered) == 2
    assert seen == ['/jobs']
