import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import Error as BrowserError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Source/bots"))
import runner


@pytest.fixture(autouse=True)
def no_browser_in_cli_unit_tests(monkeypatch):
    session = AsyncMock()
    monkeypatch.setattr(runner, "BrowserSession", Mock(return_value=session))


def test_file_batch_preserves_order_and_visits_by_default(tmp_path, monkeypatch):
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps([{"query": "first", "domain": "example.com"}, {"query": "second"}]))
    async def run(task, **kwargs):
        assert kwargs["click"] is True
        return runner.Result(task.query, task.domain, status="found")
    fake = AsyncMock(side_effect=run)
    monkeypatch.setattr(runner, "run_task", fake)
    assert runner.main(["--tasks-file", str(tasks), "--repeat", "2"]) == 0
    assert [c.args[0].query for c in fake.call_args_list] == ["first", "first", "second", "second"]


def test_batch_stops_on_unresolved_captcha(tmp_path, monkeypatch):
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps([{"query": "first"}, {"query": "second"}]))
    fake = AsyncMock(return_value=runner.Result("first", None, status="captcha"))
    monkeypatch.setattr(runner, "run_task", fake)
    assert runner.main(["--tasks-file", str(tasks), "--repeat", "2"]) == 1
    assert fake.await_count == 1


@pytest.mark.parametrize('status', ['captcha', 'timeout'])
def test_captcha_does_not_open_inspection_window_even_with_keep_open(monkeypatch, tmp_path, status):
    monkeypatch.setattr(runner, 'run_task', AsyncMock(return_value=runner.Result('query', None, status=status)))
    key = tmp_path/'key'; key.write_text('test-key')
    assert runner.main(['--query','query','--headed','--keep-open-on-failure','--captcha-key-file',str(key),'--output',str(tmp_path)]) == 1
    runner.BrowserSession.return_value.__aenter__.return_value.browser.on.assert_not_called()


def test_captcha_redirect_does_not_abort_task():
    import asyncio
    page = Mock()
    page.url = "https://ya.ru/search/"
    page.title = AsyncMock(side_effect=BrowserError("Execution context was destroyed, most likely because of a navigation"))
    page.is_closed.return_value = False
    assert asyncio.run(runner.captcha_present(page)) is True


def test_upload_requires_explicit_service_and_client():
    with pytest.raises(SystemExit) as exc:
        runner.main(["--query", "test", "--upload-stats"])
    assert exc.value.code == 2


def test_manual_task_can_submit_report_without_fetching(monkeypatch, tmp_path):
    import task_service
    monkeypatch.setenv("BOT_SERVICE_KEY", "sandbox-key")
    client = Mock()
    client.submit.return_value = True
    client.last_response = {"http_status": 200, "body": "OK"}
    monkeypatch.setattr(task_service, "TaskService", Mock(return_value=client))
    fake = AsyncMock(return_value=runner.Result("query", "example.com", status="found"))
    monkeypatch.setattr(runner, "run_task", fake)
    assert runner.main(["--query", "query", "--domain", "example.com", "--upload-stats", "--client-id", "sandbox", "--no-proxy", "--output", str(tmp_path)]) == 0
    client.fetch.assert_not_called()
    assert client.submit.call_args.args[1] == "sandbox"
    receipt = json.loads(next(tmp_path.glob("submission-*.json")).read_text())
    assert receipt["submitted"] is True and receipt["response"]["http_status"] == 200


def test_no_proxy_overrides_environment(monkeypatch):
    monkeypatch.setenv("BROWSER_PROXY_SERVER", "http://unused.invalid:8080")
    fake = AsyncMock(return_value=runner.Result("query", None, status="found"))
    monkeypatch.setattr(runner, "run_task", fake)
    assert runner.main(["--query", "query", "--no-proxy"]) == 0
    assert fake.call_args.kwargs["proxy"] is None
    assert runner.BrowserSession.call_args.kwargs["proxy"] is None


def test_browser_startup_failure_is_saved_without_credentials(tmp_path):
    runner.BrowserSession.return_value.__aenter__.side_effect = BrowserError("net::ERR_TIMED_OUT at https://user:secret@host")
    assert runner.main(["--query", "query", "--no-proxy", "--output", str(tmp_path)]) == 2
    record = json.loads(next(tmp_path.glob("batch-error-*.json")).read_text())
    assert record["error"] == "net::ERR_TIMED_OUT"
    assert record["completed_tasks"] == 0
    assert "secret" not in json.dumps(record)
    assert record['stage'] == 'browser_start'
    assert record['error_type'] == 'Error'
    assert 'net::ERR_TIMED_OUT' in record['message']


def test_submit_failure_reports_stage_and_keeps_completed_results(monkeypatch, tmp_path):
    import task_service
    monkeypatch.setenv('BOT_SERVICE_KEY', 'private-key')
    client = Mock()
    client.key = 'private-key'
    client.submit.side_effect = RuntimeError('Backend refused private-key')
    monkeypatch.setattr(task_service, 'TaskService', Mock(return_value=client))
    monkeypatch.setattr(runner, 'run_task', AsyncMock(return_value=runner.Result('query', 'example.com', status='found')))
    assert runner.main(['--query','query','--domain','example.com','--upload-stats','--client-id','test','--output',str(tmp_path)]) == 2
    failure = json.loads(next(tmp_path.glob('batch-error-*.json')).read_text())
    assert failure['stage'] == 'submit_stats' and failure['completed_tasks'] == 1
    assert failure['message'] == 'Backend refused <redacted>'


def test_task_startup_error_has_separate_readable_diagnostic(tmp_path):
    import asyncio
    runner.BrowserSession.return_value.__aenter__.side_effect = BrowserError('Chrome executable does not exist at /missing/chrome')
    result = asyncio.run(runner.run_task(runner.Task('query'), output=tmp_path))
    failure = json.loads((Path(result.artifacts)/'task-error.json').read_text())
    assert result.status == 'error'
    assert failure['stage'] == 'browser_start'
    assert 'executable does not exist' in failure['message']


def test_wait_for_empty_server_expires_without_browser_or_submission(monkeypatch, tmp_path):
    import task_service
    monkeypatch.setenv('BOT_SERVICE_KEY', 'sandbox-key')
    client = Mock()
    client.fetch.return_value = []
    client.last_response = {'http_status': 200, 'body': ''}
    monkeypatch.setattr(task_service, 'TaskService', Mock(return_value=client))
    assert runner.main(['--fetch-tasks', '--wait-for-tasks', '0.01', '--output', str(tmp_path)]) == 2
    runner.BrowserSession.assert_not_called()
    client.submit.assert_not_called()


def test_paid_solver_is_opt_in_and_injected_without_requests(monkeypatch, tmp_path):
    import captcha_service
    from captcha_handler import PaidSearchCaptchaHandler
    key = tmp_path / 'key.txt'
    key.write_text('local-test-key')
    service = Mock()
    service.submitted = False
    service.exhausted = False
    service.exchanges = []
    service.task_id = None
    service.__enter__ = Mock(return_value=service)
    service.__exit__ = Mock(return_value=None)
    constructor = Mock(return_value=service)
    monkeypatch.setattr(captcha_service, 'CaptchaService', constructor)
    monkeypatch.setattr(captcha_service, 'CaptchaQuota', constructor)
    fake = AsyncMock(return_value=runner.Result('query', 'example.com', status='found'))
    monkeypatch.setattr(runner, 'run_task', fake)
    assert runner.main(['--query', 'query', '--domain', 'example.com', '--captcha-key-file', str(key), '--output', str(tmp_path)]) == 0
    assert isinstance(fake.call_args.kwargs['captcha_handler'], PaidSearchCaptchaHandler)
    service.solve_image.assert_not_called()


def test_paid_budget_is_shared_across_batch_tasks(monkeypatch, tmp_path):
    import captcha_service
    tasks = tmp_path / 'tasks.json'
    tasks.write_text(json.dumps([{'query': 'first'}, {'query': 'second'}]))
    key = tmp_path / 'key.txt'
    key.write_text('local-test-key')
    service = Mock()
    service.submitted = False
    service.exhausted = False
    service.exchanges = []
    service.task_id = None
    service.__enter__ = Mock(return_value=service)
    service.__exit__ = Mock(return_value=None)
    monkeypatch.setattr(captcha_service, 'CaptchaService', Mock(return_value=service))
    monkeypatch.setattr(captcha_service, 'CaptchaQuota', Mock(return_value=service))
    handlers = []
    async def run(task, **kwargs):
        handlers.append(kwargs['captcha_handler'])
        service.submitted = True  # Simulate even an ambiguous submission failure.
        service.exhausted = True
        return runner.Result(task.query, task.domain, status='found')
    monkeypatch.setattr(runner, 'run_task', AsyncMock(side_effect=run))
    assert runner.main(['--tasks-file', str(tasks), '--captcha-key-file', str(key), '--output', str(tmp_path)]) == 0
    assert handlers[0] is not None and handlers[1] is None


def test_received_server_tasks_are_saved_before_browser_run(monkeypatch, tmp_path):
    import task_service
    monkeypatch.setenv('BOT_SERVICE_KEY', 'sandbox-key')
    client = Mock()
    client.fetch.return_value = [runner.Task('query', 'example.com')]
    client.last_response = {'http_status': 200, 'body': ''}
    monkeypatch.setattr(task_service, 'TaskService', Mock(return_value=client))
    async def run(task, **kwargs):
        assert json.loads((tmp_path / 'received-tasks.json').read_text()) == [{'query': 'query', 'domain': 'example.com'}]
        return runner.Result(task.query, task.domain, status='found')
    monkeypatch.setattr(runner, 'run_task', AsyncMock(side_effect=run))
    assert runner.main(['--fetch-tasks', '--output', str(tmp_path)]) == 0
