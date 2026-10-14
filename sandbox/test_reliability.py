import json
from pathlib import Path
import queue
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Source/bots'))
import runner
import task_service
import worker_pool
from captcha_service import CaptchaService


@pytest.mark.parametrize('option', ['deadline','captcha-wait','action-pause','visit-seconds','wait-for-tasks','task-poll-interval'])
@pytest.mark.parametrize('value', ['nan','inf'])
def test_nonfinite_time_options_fail_before_browser(monkeypatch, option, value):
    browser = Mock(); monkeypatch.setattr(runner, 'BrowserSession', browser)
    with pytest.raises(SystemExit) as error:
        runner.main(['--query','query','--'+option,value])
    assert error.value.code == 2
    browser.assert_not_called()


@pytest.mark.parametrize('option', ['deadline','result_timeout','poll_interval'])
def test_captcha_nonfinite_limits_are_rejected(option):
    with pytest.raises(ValueError):
        CaptchaService('test-key', endpoint='https://solver.invalid', **{option:float('inf')})


def test_browser_close_failure_still_submits_completed_results_once(tmp_path, monkeypatch):
    monkeypatch.setenv('BOT_SERVICE_KEY','test-key')
    session = AsyncMock()
    session.__aexit__.side_effect = RuntimeError('browser close failed')
    monkeypatch.setattr(runner, 'BrowserSession', Mock(return_value=session))
    monkeypatch.setattr(runner, 'run_task', AsyncMock(return_value=runner.Result('query','example.com',status='found')))
    service = Mock(); service.key='test-key'; service.last_response={'http_status':200,'body':''}
    service.submit.return_value=True
    monkeypatch.setattr(task_service,'TaskService',Mock(return_value=service))
    assert runner.main(['--query','query','--domain','example.com','--upload-stats','--client-id','test','--output',str(tmp_path)]) == 2
    service.submit.assert_called_once()
    failure=json.loads(next(tmp_path.glob('batch-error-*.json')).read_text())
    assert failure['stage']=='browser_close'
    assert failure['submission_after_close_error']=='completed'
    assert json.loads(next(tmp_path.glob('submission-*.json')).read_text())['submitted']


def test_private_files_are_removed_if_key_creation_fails(tmp_path, monkeypatch):
    original=worker_pool.os.open
    def fail_key(path, *args, **kwargs):
        descriptor=original(path,*args,**kwargs)
        if Path(path).name=='service-key.txt':
            worker_pool.os.close(descriptor)
            raise PermissionError('simulated key write failure')
        return descriptor
    monkeypatch.setattr(worker_pool.os,'open',fail_key)
    proxies=queue.Queue(); proxies.put(('proxy',{'server':'socks5://127.0.0.1:8000'}))
    service=SimpleNamespace(fetch=lambda:[runner.Task('query')],last_response={},session=Mock())
    launch=Mock()
    worker_pool.worker(1,proxies,threading.Event(),key='test-key',output=tmp_path,profiles=tmp_path/'profiles',
                       client_id='test',interval=0,extra=[],max_cycles=1,service_factory=lambda key:service,launch=launch)
    launch.assert_not_called()
    assert not list(tmp_path.rglob('proxy.json')) and not list(tmp_path.rglob('service-key.txt'))
    assert proxies.qsize()==1
    assert len(list(tmp_path.rglob('received-tasks.json')))==1


def test_hung_child_is_terminated_by_watchdog(tmp_path, monkeypatch):
    process=Mock(); process.pid=12345; process.poll.return_value=None
    monkeypatch.setattr(worker_pool.subprocess,'Popen',Mock(return_value=process))
    kill=Mock()
    if worker_pool.os.name!='nt':
        monkeypatch.setattr(worker_pool.os,'killpg',kill)
    else:
        monkeypatch.setattr(worker_pool.subprocess,'run',kill)
    times=iter([0,10])
    stop=Mock(); stop.wait.return_value=False
    with pytest.raises(TimeoutError,match='watchdog'):
        worker_pool.run_child(['fake'],tmp_path/'console.log',stop,timeout=1,clock=lambda:next(times))
    assert kill.call_count==1
    process.wait.assert_called_once_with(timeout=10)


def test_crashed_second_worker_stops_first_worker(tmp_path, monkeypatch):
    proxies=tmp_path/'proxies.json'; proxies.write_text(json.dumps([{'server':'socks5://127.0.0.1:8000'},{'server':'socks5://127.0.0.2:8000'}]))
    key=tmp_path/'key'; key.write_text('test-key')
    monkeypatch.setattr(worker_pool.subprocess,'run',Mock(return_value=SimpleNamespace(returncode=0)))
    stopped=threading.Event()
    def fake_worker(number, leases, stop, **kwargs):
        if number==2:
            raise RuntimeError('worker crashed')
        assert stop.wait(2), 'Other worker was not notified of the crash'
        stopped.set()
    monkeypatch.setattr(worker_pool,'worker',fake_worker)
    with pytest.raises(RuntimeError,match='worker crashed'):
        worker_pool.main(['--workers','2','--proxies-file',str(proxies),'--service-key-file',str(key),
                          '--captcha-key-file',str(key),'--output',str(tmp_path/'logs')])
    assert stopped.is_set()


def test_failed_captcha_metadata_write_still_closes_clients(tmp_path, monkeypatch):
    import captcha_service
    key=tmp_path/'key'; key.write_text('test-key')
    monkeypatch.setenv('BOT_SERVICE_KEY','test-key')
    monkeypatch.setattr(runner,'BrowserSession',Mock(return_value=AsyncMock()))
    monkeypatch.setattr(runner,'run_task',AsyncMock(return_value=runner.Result('query','example.com',status='found')))
    solver=Mock(); solver.submitted=False; solver.task_id=None; solver.exchanges=[]; solver.exhausted=False
    solver.__exit__=Mock()
    monkeypatch.setattr(captcha_service,'CaptchaQuota',Mock(return_value=solver))
    service=Mock(); service.last_response={'http_status':200,'body':''}; service.submit.return_value=True
    monkeypatch.setattr(task_service,'TaskService',Mock(return_value=service))
    original=Path.write_text
    def write(path,*args,**kwargs):
        if path.name=='captcha-service.json': raise OSError('disk full')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'write_text',write)
    assert runner.main(['--query','query','--captcha-key-file',str(key),'--upload-stats',
                        '--client-id','test','--output',str(tmp_path)])==0
    solver.__exit__.assert_called_once()
    service.session.close.assert_called_once()
