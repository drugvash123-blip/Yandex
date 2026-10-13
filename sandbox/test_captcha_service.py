import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Source/bots'))
from captcha_service import CaptchaService, CaptchaError, CaptchaTimeout, parse_reply, parse_coordinates


class FakeSession:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(text=reply, status_code=200, is_redirect=False, raise_for_status=lambda: None)


def client(replies, **kwargs):
    session = FakeSession(replies)
    now = [0]
    def sleep(seconds):
        now[0] += seconds
    service = CaptchaService('test-secret', endpoint='https://solver.invalid', session=session,
                             clock=lambda: now[0], sleep=sleep, **kwargs)
    return service, session


@pytest.mark.parametrize('reply', ['CAPCHA_NOT_READY', 'CAPTCHA_NOT_READY', '{"status":0,"request":"CAPCHA_NOT_READY"}'])
def test_pending(reply):
    assert parse_reply(reply) == (False, None)


@pytest.mark.parametrize('reply', ['OK|answer|with-pipe', '{"status":1,"request":"answer|with-pipe"}'])
def test_success_reply(reply):
    assert parse_reply(reply) == (True, 'answer|with-pipe')


@pytest.mark.parametrize('reply,code', [('ERROR_ZERO_BALANCE', 'ERROR_ZERO_BALANCE'),
                                     ('ERROR_CAPTCHA_UNSOLVABLE', 'ERROR_CAPTCHA_UNSOLVABLE'),
                                     ('<html>test-secret</html>', 'invalid_response'),
                                     ('OK|', 'invalid_response'), ('{broken', 'invalid_response')])
def test_errors_are_safe(reply, code):
    with pytest.raises(CaptchaError, match=code) as error:
        parse_reply(reply)
    assert 'test-secret' not in str(error.value)


def test_solve_and_single_submission():
    service, session = client(['OK|123', 'CAPCHA_NOT_READY', '{"status":1,"request":"word"}'])
    assert session.calls == []
    assert service.solve_image(b'image') == 'word'
    assert len(session.calls) == 3
    assert service.exchanges == [{'phase': 'submit', 'http_status': 200}, {'phase': 'poll', 'http_status': 200}, {'phase': 'poll', 'http_status': 200}]
    assert session.calls[0][1]['json']['body'] == 'aW1hZ2U='
    assert all(call[1]['allow_redirects'] is False for call in session.calls)
    with pytest.raises(CaptchaError, match='submission_limit_reached'):
        service.solve_image(b'image')
    assert len(session.calls) == 3


def test_poll_limit():
    service, session = client(['OK|123', 'CAPCHA_NOT_READY', 'CAPCHA_NOT_READY'], max_polls=2)
    with pytest.raises(CaptchaTimeout, match='poll_limit_reached'):
        service.solve_image(b'image')
    assert len(session.calls) == 3


def test_deadline():
    service, session = client(['OK|123', 'CAPCHA_NOT_READY'], deadline=15)
    with pytest.raises(CaptchaTimeout, match='deadline_exceeded'):
        service.solve_image(b'image')
    assert len(session.calls) == 2


def test_ambiguous_failure_does_not_resubmit_or_leak_key():
    service, session = client([requests.Timeout('request included test-secret')])
    with pytest.raises(CaptchaError, match='transport_error') as error:
        service.solve_image(b'image')
    assert 'test-secret' not in str(error.value)
    with pytest.raises(CaptchaError, match='submission_limit_reached'):
        service.solve_image(b'image')
    assert len(session.calls) == 1
    assert service.exchanges == [{'phase': 'submit', 'error_type': 'Timeout'}]


def test_bad_task_id_stops_before_polling():
    service, session = client(['OK|not-an-id'])
    with pytest.raises(CaptchaError, match='invalid_task_id'):
        service.solve_image(b'image')
    assert len(session.calls) == 1


@pytest.mark.parametrize('prefix', ['coordinate:', 'coordinates:'])
def test_coordinates(prefix):
    assert parse_coordinates(prefix + 'x=0,y=0;x=99,y=49', 100, 50) == [(0, 0), (99, 49)]


@pytest.mark.parametrize('answer', ['coordinate:x=100,y=0', 'coordinate:x=-1,y=0', 'coordinate:', 'OK|x=1,y=2', 'coordinate:x=1,y=2;broken'])
def test_invalid_coordinates(answer):
    with pytest.raises(CaptchaError):
        parse_coordinates(answer, 100, 50)


def test_https_required():
    with pytest.raises(ValueError):
        CaptchaService('test-secret', endpoint='http://solver.invalid')


def test_legacy_polling_compatibility(monkeypatch):
    import timeLib
    now = [0]
    calls = []
    replies = iter(['CAPCHA_NOT_READY', 'OK|word'])
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(text=next(replies), is_redirect=False, raise_for_status=lambda: None)
    monkeypatch.setattr(timeLib.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(timeLib.time, 'sleep', lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(timeLib.requests, 'get', get)
    assert timeLib.wait_captcha_response('test-secret', '123') == 'OK|word'
    assert len(calls) == 2 and now[0] == 20
    assert all(url == 'https://api.cap.guru/res.php' for url, _ in calls)


def test_legacy_polling_stops(monkeypatch):
    import timeLib
    now = [0]
    monkeypatch.setattr(timeLib.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(timeLib.time, 'sleep', lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(timeLib.requests, 'get', lambda *args, **kwargs: SimpleNamespace(
        text='CAPCHA_NOT_READY', is_redirect=False, raise_for_status=lambda: None))
    with pytest.raises(CaptchaTimeout):
        timeLib.wait_captcha_response('test-secret', '123', timeout=15)


def test_transient_poll_failure_reuses_accepted_task_without_submission():
    service, session = client(['OK|123', requests.ConnectionError('included test-secret'), 'OK|answer'])
    assert service.solve_image(b'image') == 'answer'
    assert service.task_id == '123'
    assert [url.rsplit('/', 1)[-1] for url, _ in session.calls] == ['in.php', 'res.php', 'res.php']
    assert [kwargs['json']['id'] for _, kwargs in session.calls[1:]] == ['123', '123']
    assert 'test-secret' not in str(service.exchanges)


def test_transient_poll_failures_remain_bounded():
    service, session = client(['OK|123', requests.ConnectionError(), requests.Timeout()], max_polls=2)
    with pytest.raises(CaptchaError, match='transport_error'):
        service.solve_image(b'image')
    assert service.task_id == '123'
    assert len(session.calls) == 3


def test_quota_consumes_ambiguous_submission_and_bounds_whole_batch():
    from captcha_service import CaptchaQuota
    created = []
    def factory(key, endpoint):
        service, session = client([requests.Timeout('uncertain')])
        created.append(session)
        return service
    quota = CaptchaQuota('test-secret', endpoint='https://solver.invalid', max_tasks=1, factory=factory)
    with pytest.raises(CaptchaError, match='transport_error'):
        quota.solve_image(b'image')
    assert quota.exhausted and quota.remaining == 0
    with pytest.raises(CaptchaError, match='submission_limit_reached'):
        quota.solve_image(b'another')
    assert len(created) == 1 and len(created[0].calls) == 1


def test_explicit_quota_uses_separate_single_submission_clients():
    from captcha_service import CaptchaQuota
    created = []
    def factory(key, endpoint):
        service, session = client(['OK|' + str(len(created) + 1), 'OK|word'])
        created.append(session)
        return service
    quota = CaptchaQuota('test-secret', endpoint='https://solver.invalid', max_tasks=2, factory=factory)
    assert quota.solve_image(b'first') == 'word'
    assert quota.solve_image(b'new challenge') == 'word'
    assert quota.exhausted
    assert quota.task_id == '2'
    assert all(len(session.calls) == 2 for session in created)


def test_unlimited_quota_allows_more_than_three_separate_challenges():
    from captcha_service import CaptchaQuota
    created = []
    def factory(key, endpoint):
        service, session = client(['OK|' + str(len(created) + 1), 'OK|word'])
        created.append(session)
        return service
    quota = CaptchaQuota('test-secret', endpoint='https://solver.invalid', max_tasks=0, factory=factory)
    for _ in range(5):
        assert quota.solve_image(b'new challenge') == 'word'
    assert not quota.exhausted and quota.remaining == float('inf')
    assert quota.task_id == '5'
    assert all(len(session.calls) == 2 for session in created)


def test_quota_admission_remains_bounded_for_concurrent_callers():
    from captcha_service import CaptchaQuota
    from concurrent.futures import ThreadPoolExecutor
    import time
    created = []
    def factory(key, endpoint):
        time.sleep(0.03)
        service, session = client(['OK|123', 'OK|word'])
        created.append(session)
        return service
    quota = CaptchaQuota('test-secret', endpoint='https://solver.invalid', factory=factory)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(quota.solve_image, b'image') for _ in range(2)]
        successful = 0
        for future in futures:
            try:
                assert future.result() == 'word'
                successful += 1
            except CaptchaError as error:
                assert str(error) == 'submission_limit_reached'
    assert successful == 1 and len(created) == 1
