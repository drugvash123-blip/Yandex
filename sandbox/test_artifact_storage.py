from pathlib import Path
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import asyncio

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'Source/bots'))
from artifact_storage import prune_completed
from diagnostics import ARTIFACT_MODE, rotating_log
import runner


def batch(root, number, size, complete=True):
    p=root/'worker-1'/f'{number:032x}'; p.mkdir(parents=True)
    (p/'data.bin').write_bytes(b'x'*size)
    if complete:
        (p/'.complete').touch(); os.utime(p/'.complete',(number,number))
    return p


def test_retention_removes_oldest_completed_but_protects_active(tmp_path):
    old=batch(tmp_path,1,100); recent=batch(tmp_path,2,100); active=batch(tmp_path,3,1000,False)
    assert prune_completed(tmp_path,max_bytes=150,max_batches=100)==[old.name]
    assert recent.exists() and active.exists()


def test_count_limit_and_old_release_receipts(tmp_path):
    old=batch(tmp_path,1,1,False); chunk=old/'chunk-1'; chunk.mkdir(); (chunk/'submission-test.json').write_text('{}')
    recent=batch(tmp_path,2,1)
    os.utime(old,(1,1))
    assert prune_completed(tmp_path,max_bytes=100,max_batches=1)==[old.name]
    assert recent.exists()


def test_compact_mode_never_screenshots_or_starts_tracing(tmp_path,monkeypatch):
    context=Mock(); context.pages=[]; context.tracing=SimpleNamespace(start=AsyncMock(),stop=AsyncMock())
    page=Mock(); context.new_page=AsyncMock(return_value=page)
    session=SimpleNamespace(context=context,metadata=AsyncMock(return_value={}))
    async def search(*args): args[2].status='found'
    monkeypatch.setattr(runner,'search',search)
    result=asyncio.run(runner.run_task(runner.Task('query'),session=session,output=tmp_path,diagnostics='compact'))
    assert result.status=='found'
    context.tracing.start.assert_not_awaited()
    context.tracing.stop.assert_not_awaited()
    assert not list(tmp_path.rglob('*.zip')) and not list(tmp_path.rglob('*.png'))
    assert ARTIFACT_MODE.get()=='full'  # Scope is reset after the task.


def test_log_rotation_is_bounded(tmp_path):
    import logging
    handler=rotating_log(tmp_path/'test.log',max_bytes=100,backups=2)
    for _ in range(100): handler.emit(logging.LogRecord('test',logging.INFO,'',0,'x'*40,(),None))
    handler.close()
    files=list(tmp_path.glob('test.log*'))
    assert len(files)==3 and sum(p.stat().st_size for p in files)<=300
