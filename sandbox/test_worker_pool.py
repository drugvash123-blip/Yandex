import json
from pathlib import Path
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Source/bots'))
import worker_pool
from runner import Task


def leases(count):
    result = queue.Queue()
    for i in range(count):
        result.put((str(i), {'server': f'socks5://127.0.0.{i+1}:8000', 'username': 'u', 'password': 'secret'}))
    return result


def test_concurrent_workers_rotate_proxies_without_sharing_profiles(tmp_path):
    proxies = leases(3)
    active, observed, threads = set(), [], set()
    lock = threading.Lock()
    barrier = threading.Barrier(2)
    services = []
    def factory(key):
        service = SimpleNamespace(fetch=lambda: [Task('query', 'example.com')],
                                  last_response={'http_status': 200, 'body': 'query | example.com'}, session=Mock())
        services.append(service)
        return service
    def launch(command, console, stop):
        def value(flag):
            return command[command.index(flag)+1]
        profile = value('--profile')
        with lock:
            assert profile not in active
            active.add(profile)
            observed.append(profile)
            threads.add(threading.get_ident())
            first = len(observed) <= 2
        if first:
            barrier.wait(timeout=3)
        assert '--upload-stats' in command
        assert json.loads(Path(value('--tasks-file')).read_text())[0]['query'] == 'query'
        assert json.loads(Path(value('--proxy-config')).read_text())['password'] == 'secret'
        assert Path(value('--service-key-file')).read_text() == 'key'
        assert 'secret' not in ' '.join(command) and 'key' not in command
        time.sleep(.01)
        with lock:
            active.remove(profile)
        return 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker_pool.worker, i, proxies, threading.Event(), key='key',
                               output=tmp_path, profiles=tmp_path/'profiles', client_id='test',
                               interval=0, extra=['--headed'], max_cycles=3,
                               service_factory=factory, launch=launch) for i in range(2)]
        for future in futures:
            future.result()
    assert len(threads) == 2 and len(observed) == 6 and len(set(observed)) == 3
    assert proxies.qsize() == 3 and proxies.unfinished_tasks == 3
    assert len(list(tmp_path.rglob('received-tasks.json'))) == 6
    assert not list(tmp_path.rglob('proxy.json')) and not list(tmp_path.rglob('service-key.txt'))
    assert all(service.session.close.call_count == 1 for service in services)


def test_empty_and_failed_fetch_continue_then_run_task(tmp_path):
    replies = iter([[], ValueError('backend failed'), [Task('query')]])
    def fetch():
        value = next(replies)
        if isinstance(value, Exception):
            raise value
        return value
    service = SimpleNamespace(fetch=fetch, last_response={}, session=Mock())
    launch = Mock(return_value=1)
    proxies = leases(2)
    worker_pool.worker(1, proxies, threading.Event(), key='key', output=tmp_path,
                       profiles=tmp_path/'profiles', client_id='test', interval=0, extra=[],
                       max_cycles=3, service_factory=lambda key: service, launch=launch)
    assert launch.call_count == 1 and proxies.qsize() == 2
    assert len(list(tmp_path.rglob('received-tasks.json'))) == 1


def test_stop_terminates_and_reaps_child(tmp_path, monkeypatch):
    process = Mock()
    process.pid = 12345
    process.poll.return_value = None
    monkeypatch.setattr(worker_pool.subprocess, 'Popen', Mock(return_value=process))
    kill_group = Mock()
    if worker_pool.os.name != 'nt':
        monkeypatch.setattr(worker_pool.os, 'killpg', kill_group)
    else:
        monkeypatch.setattr(worker_pool.subprocess, 'run', Mock())
    stop = threading.Event(); stop.set()
    worker_pool.run_child(['fake'], tmp_path/'console.log', stop)
    if worker_pool.os.name != 'nt':
        kill_group.assert_called_once_with(process.pid, worker_pool.signal.SIGTERM)
    process.wait.assert_called_once_with(timeout=10)


def test_stop_during_fetch_retains_received_tasks_without_starting_browser(tmp_path):
    stop = threading.Event()
    def fetch():
        stop.set()
        return [Task('received before stop')]
    service = SimpleNamespace(fetch=fetch, last_response={}, session=Mock())
    launch = Mock()
    worker_pool.worker(1, leases(1), stop, key='key', output=tmp_path,
                       profiles=tmp_path/'profiles', client_id='test', interval=0, extra=[],
                       service_factory=lambda key: service, launch=launch)
    launch.assert_not_called()
    saved = next(tmp_path.rglob('received-tasks.json'))
    assert json.loads(saved.read_text())[0]['query'] == 'received before stop'


def test_proxy_validation_and_converter_output(tmp_path):
    from proxy_to_json import convert
    path = tmp_path/'proxies.json'
    path.write_text(json.dumps([convert('127.0.0.1:8000@user:pass'), convert('127.0.0.2:8000@user:pass')]))
    assert len(worker_pool.load_proxies(path)) == 2
    path.write_text(json.dumps([{'server': 'socks5://127.0.0.1:8000'}]*2))
    with pytest.raises(ValueError, match='Duplicate'):
        worker_pool.load_proxies(path)


def test_invalid_browser_flags_fail_before_fetch(tmp_path, monkeypatch):
    path = tmp_path/'proxies.json'; path.write_text(json.dumps({'server':'socks5://127.0.0.1:8000'}))
    key = tmp_path/'key'; key.write_text('key')
    factory = Mock(); monkeypatch.setattr(worker_pool, 'TaskService', factory)
    with pytest.raises(SystemExit) as error:
        worker_pool.main(['--workers','2','--proxies-file',str(path),'--service-key-file',str(key),'--max-pages','0'])
    assert error.value.code == 2
    factory.assert_not_called()
