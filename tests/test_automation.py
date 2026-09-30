import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from resumebot import db
from resumebot.config import SourcePacing
from resumebot.engine import pipeline, scheduler
from resumebot.engine.pacing import Decision
from resumebot.inbox import gmail, gmail_api
from resumebot.models import utcnow


@pytest.mark.parametrize('reason', ['outside active hours', 'daily cap reached (25/25)', 'paused (cooldown)', 'manual mode'])
async def test_automatic_tick_respects_gate(monkeypatch, reason):
    gate = SimpleNamespace(clear_expired=Mock(), check=lambda: Decision(False, reason))
    monkeypatch.setattr(pipeline, 'Gate', lambda name: gate)
    monkeypatch.setattr(pipeline, 'next_job', Mock(side_effect=AssertionError('Must not pick a job')))
    db.kv_set('run_now', True)
    assert await pipeline.tick_source('greenhouse') == f'wait: {reason}'
    db.kv_set('run_now', False)


async def test_automatic_tick_processes_one_job_and_records_pacing(monkeypatch):
    gate = SimpleNamespace(clear_expired=Mock(), check=lambda: Decision(True),
                           cfg=SourcePacing(browse_only_ratio=0), record_action=Mock())
    monkeypatch.setattr(pipeline, 'Gate', lambda name: gate)
    job = SimpleNamespace(id=42, title="Test role", company="Test", status="applied", status_reason="Confirmed")
    monkeypatch.setattr(pipeline, 'next_job', lambda name: job)
    apply = AsyncMock()
    monkeypatch.setattr(pipeline, 'apply_job', apply)
    assert await pipeline.tick_source('greenhouse') == '#42: applied — Confirmed'
    apply.assert_awaited_once_with(job)
    gate.record_action.assert_called_once()


async def test_guard_recovers_after_failure(monkeypatch):
    calls = []
    async def work():
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError('Test transient scheduler failure')
        raise asyncio.CancelledError()
    monkeypatch.setattr(scheduler.asyncio, 'sleep', AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await scheduler._guard('test', work, 30)
    assert len(calls) == 2


async def test_global_pause_blocks_application_worker(monkeypatch):
    async def once(name, run, interval):
        await run()
    monkeypatch.setattr(scheduler, '_guard', once)
    tick = AsyncMock()
    monkeypatch.setattr(pipeline, 'tick_source', tick)
    db.kv_set('global_pause', True)
    try:
        await scheduler.source_loop('greenhouse')
        tick.assert_not_awaited()
    finally:
        db.kv_set('global_pause', False)


async def test_discovery_only_checks_due_enabled_sources(monkeypatch):
    configs = {'greenhouse': SourcePacing(), 'lever': SourcePacing(enabled=False),
               'linkedin': SourcePacing(), 'ashby': SourcePacing(), 'workday': SourcePacing(mode='manual')}
    monkeypatch.setattr(scheduler, 'settings', lambda: SimpleNamespace(pacing=SimpleNamespace(sources=configs)))
    monkeypatch.setattr(db, 'source_state', lambda name: SimpleNamespace(paused=False,
        last_discover_at=utcnow() if name == 'ashby' else None))
    monkeypatch.setattr(scheduler, 'Gate', lambda name: SimpleNamespace(check=lambda: Decision(False, 'outside active hours')))
    async def once(name, run, interval):
        await run()
    monkeypatch.setattr(scheduler, '_guard', once)
    discover = AsyncMock()
    monkeypatch.setattr(pipeline, 'discover', discover)
    await scheduler.discovery_loop()
    discover.assert_awaited_once_with('greenhouse')


async def test_automated_start_launches_expected_workers(monkeypatch):
    async def idle(*args):
        await asyncio.Event().wait()
    for name in ('discovery_loop', 'triage_loop', 'inbox_loop', 'daily_summary_loop', 'source_loop'):
        monkeypatch.setattr(scheduler, name, idle)
    monkeypatch.setattr(scheduler.telegram, 'poll_forever', idle)
    monkeypatch.setattr(scheduler.telegram, 'send', AsyncMock())
    tasks = await scheduler.run_all()
    try:
        names = {task.get_name() for task in tasks}
        assert {'discovery', 'triage', 'inbox', 'telegram', 'summary', 'apply:greenhouse'} <= names
        assert 'apply:workday' not in names
        assert all(not task.done() for task in tasks)
    finally:
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_inbox_does_not_lose_reply_when_processing_fails(monkeypatch):
    db.kv_set('gmail_last_ts', 1000)
    monkeypatch.setattr(gmail_api, 'fetch_since', lambda ts: [(2000, 'sender', 'subject', 'body')])
    process = AsyncMock(side_effect=RuntimeError('Temporary classifier failure'))
    monkeypatch.setattr(gmail, '_process', process)
    with pytest.raises(RuntimeError):
        await gmail._check_api(gmail_api)
    assert db.kv_get('gmail_last_ts') == 1000
    process.side_effect = None
    await gmail._check_api(gmail_api)
    assert db.kv_get('gmail_last_ts') == 2000


async def test_imap_retries_failed_processing(monkeypatch):
    db.kv_set('gmail_last_uid', 10)
    monkeypatch.setattr(gmail, 'fetch_new', lambda uid: [(11, 'sender', 'subject', 'body')])
    process = AsyncMock(side_effect=RuntimeError('Temporary failure'))
    monkeypatch.setattr(gmail, '_process', process)
    with pytest.raises(RuntimeError):
        await gmail._check_imap()
    assert db.kv_get('gmail_last_uid') == 10
    process.side_effect = None
    await gmail._check_imap()
    assert db.kv_get('gmail_last_uid') == 11


async def test_runtime_reports_stopped_worker():
    from resumebot.engine import runtime
    async def idle():
        await asyncio.Event().wait()
    task = asyncio.create_task(idle(), name='triage')
    runtime.register([task])
    try:
        runtime.update('triage', phase='running', message='Scoring example role')
        assert runtime.snapshot()[0]['message'] == 'Scoring example role'
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert runtime.snapshot()[0]['phase'] == 'stopped'
    finally:
        runtime.TASKS.clear()
        runtime.STATES.clear()


async def test_visibility_shows_queue_counts_and_live_worker():
    import httpx
    from uuid import uuid4
    from resumebot.engine import runtime
    from resumebot.models import Job
    from resumebot.web.app import app
    async def idle():
        await asyncio.Event().wait()
    task = asyncio.create_task(idle(), name='triage')
    runtime.register([task])
    runtime.update('triage', phase='running', message='Scoring the next job')
    db.kv_set('automatic_mode', True)
    db.save(Job(source='greenhouse', external_id=uuid4().hex, title='Visible queued job', company='Test',
                url='https://example.com', status='queued'))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get('/applications')
            assert response.status_code == 200
            for text in ['Scoring the next job', 'Visible queued job', 'Why applications are waiting', 'Application history']:
                assert text in response.text
            payload = (await client.get('/api/automation')).json()
            assert payload['live'] and payload['counts']['queued'] >= 1
            assert (await client.get('/partials/automation')).status_code == 200
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        runtime.TASKS.clear()
        runtime.STATES.clear()
        db.kv_set('automatic_mode', False)


async def test_screening_prioritizes_target_keywords(monkeypatch):
    from uuid import uuid4
    from resumebot.models import Job
    db.save(Job(source='ashby', external_id=uuid4().hex, title='Risk Program Manager', company='Test', url='u'))
    priority = db.save(Job(source='ashby', external_id=uuid4().hex, title='Security Analyst', company='Test', url='u'))
    seen = []
    def prefilter(job):
        seen.append(job.id)
        return False, 'Test: skip without any application'
    monkeypatch.setattr(pipeline.matcher, 'prefilter', prefilter)
    await pipeline.triage_new(limit=1)
    assert seen == [priority.id]
