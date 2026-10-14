import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Source/bots'))
from captcha_handler import PaidSearchCaptchaHandler
from captcha_service import CaptchaError


def test_browser_failure_with_observed_new_image_continues_stage(monkeypatch):
    import captcha_handler
    from unittest.mock import AsyncMock
    state = {'state': 'ready', 'candidates': [{'kind': 'silhouette', 'frame_index': 0}]}
    monkeypatch.setattr(captcha_handler, 'inspect_captcha', AsyncMock(return_value=state))
    monkeypatch.setattr(captcha_handler, 'solve_challenge', AsyncMock(side_effect=CaptchaError('browser_action_failed')))
    images = SimpleNamespace(evaluate_all=AsyncMock(side_effect=['old image', 'new image']))
    page = SimpleNamespace(locator=lambda selector: images)
    outcome = asyncio.run(PaidSearchCaptchaHandler(SimpleNamespace())(page))
    assert outcome['outcome'] == 'challenge_changed'
    assert outcome['search_verified'] is False


@pytest.mark.parametrize('framed', [False, True])
def test_delayed_checkbox_is_clicked_once(monkeypatch, framed):
    import captcha_handler
    import runner
    from unittest.mock import AsyncMock
    states = iter([
        {'state': 'changing', 'candidates': []},
        {'state': 'not_ready', 'candidates': [{'kind': 'checkbox', 'frame_index': int(framed), 'frame_title': 'SmartCaptcha checkbox' if framed else None}]},
        {'state': 'unrecognized', 'candidates': []},
    ])
    async def inspect(page):
        return next(states)
    monkeypatch.setattr(captcha_handler, 'inspect_captcha', inspect)
    monkeypatch.setattr(runner, 'captcha_present', AsyncMock(return_value=False))
    button = SimpleNamespace(count=AsyncMock(return_value=1), click=AsyncMock())
    scope = SimpleNamespace(locator=lambda selector: button)
    page = SimpleNamespace(locator=lambda selector: button, frames=[None, scope])
    service = SimpleNamespace(solve_image=lambda *args, **kwargs: pytest.fail('No paid image task for checkbox'))
    outcome = asyncio.run(PaidSearchCaptchaHandler(service, action_pause=0)(page))
    assert outcome['outcome'] == 'captcha_left'
    button.click.assert_awaited_once()


@pytest.mark.parametrize('kind', ['text', 'silhouette', 'silhouette-plural', 'bad-coordinates'])
def test_paid_handler_options_and_click_bounds(kind):
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel='chrome', headless=True, args=['--no-proxy-server'])
            context = await browser.new_context(offline=True)
            try:
                page = await context.new_page()
                cls = 'AdvancedCaptcha' + (' AdvancedCaptcha_silhouette' if kind != 'text' else '')
                field = '<div class="Textinput"><input></div>' if kind == 'text' else ''
                await page.set_content(f'''<div class="{cls}" style="width:100px"><div class="AdvancedCaptcha-View" style="width:100px;height:50px" onclick="window.clicks=(window.clicks||0)+1"><img src="data:image/svg+xml,%3Csvg%20xmlns=%22http://www.w3.org/2000/svg%22%20width=%22100%22%20height=%2250%22/%3E"></div>{field}<button class="CaptchaButton_view_action" onclick="window.submits=(window.submits||0)+1;document.querySelector('#done').hidden=false">Check</button></div><p id="done" hidden>Done</p>''')
                calls = []
                def solve(image, **options):
                    calls.append(options)
                    assert image.startswith(b'\x89PNG')
                    if kind == 'text':
                        return 'answer'
                    if kind == 'silhouette-plural':
                        return 'coordinates:x=10,y=10'
                    return 'coordinate:x=10,y=10' if kind == 'silhouette' else 'coordinate:x=10,y=100'
                handler = PaidSearchCaptchaHandler(SimpleNamespace(solve_image=solve), outcome_selector='#done', timeout=3, action_pause=0, typing_delay_ms=0)
                if kind == 'bad-coordinates':
                    with pytest.raises(CaptchaError):
                        await handler(page)
                    assert await page.evaluate('window.clicks||0') == 0
                    assert await page.evaluate('window.submits||0') == 0
                else:
                    await handler(page)
                    assert await page.evaluate('window.submits') == 1
                assert calls == ([{'vernet': 18}] if kind == 'text' else [{'click': 'oth', 'instructions': 'yandex'}])
            finally:
                await browser.close()
    asyncio.run(run())


def test_task_wait_returns_real_nonempty_batch():
    from runner import wait_for_tasks, Task
    replies = iter([[], [Task('query', 'example.com')]])
    service = SimpleNamespace(fetch=lambda: next(replies))
    tasks = asyncio.run(wait_for_tasks(service, 1, 0.01))
    assert tasks == [Task('query', 'example.com')]


@pytest.mark.parametrize('failure', [False, True])
def test_new_stages_only_continue_with_explicit_remaining_budget(monkeypatch, failure):
    import runner
    from unittest.mock import AsyncMock
    monkeypatch.setattr(runner, 'captcha_present', AsyncMock(return_value=True))
    class Handler:
        remaining_attempts = 3
        calls = 0
        async def __call__(self, page):
            self.calls += 1
            self.remaining_attempts -= 1
            if failure:
                raise CaptchaError('transport_error')
            return {'outcome': 'challenge_changed'}
    handler = Handler()
    result = runner.Result('query', 'example.com')
    assert asyncio.run(runner.wait_captcha(None, result, 0, handler, runner.CaptchaBudget())) is False
    assert handler.calls == (1 if failure else 3)
    assert result.status == 'captcha'  # Budget exhaustion never pretends success.


@pytest.mark.parametrize('code', ['ERROR_CAPTCHA_UNSOLVABLE', 'answer_timeout', 'poll_limit_reached'])
def test_failed_recognition_refreshes_until_quota_exhausted(monkeypatch, code):
    import runner
    from unittest.mock import AsyncMock
    monkeypatch.setattr(runner, 'captcha_present', AsyncMock(return_value=True))
    monkeypatch.setattr(runner.asyncio, 'sleep', AsyncMock())
    class Handler:
        remaining_attempts = 3
        calls = 0
        refresh_challenge = AsyncMock()
        async def __call__(self, page):
            self.calls += 1
            self.remaining_attempts -= 1
            raise CaptchaError(code)
    handler = Handler()
    result = runner.Result('query', 'example.com')
    assert asyncio.run(runner.wait_captcha(None, result, 0, handler, runner.CaptchaBudget())) is False
    assert handler.calls == 3
    assert handler.refresh_challenge.await_count == 2
    assert result.status == 'captcha'


def test_delayed_readiness_beyond_sixty_seconds_does_not_submit_early(monkeypatch):
    import captcha_handler
    from unittest.mock import AsyncMock, Mock
    service = SimpleNamespace(solve_image=Mock(return_value='answer'))
    inspections = 0
    async def inspect(page):
        nonlocal inspections
        inspections += 1
        assert service.solve_image.call_count == 0
        return {'state': 'ready' if inspections > 125 else 'changing',
                'candidates': [{'kind': 'text', 'frame_index': 0}]}
    monkeypatch.setattr(captcha_handler, 'inspect_captcha', inspect)
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    async def solve(page, spec, callback, **kwargs):
        assert inspections == 126
        assert await callback(b'fake-png') == 'answer'
    monkeypatch.setattr(captcha_handler, 'solve_challenge', solve)
    images = SimpleNamespace(evaluate_all=AsyncMock(return_value='image'))
    asyncio.run(PaidSearchCaptchaHandler(service)(SimpleNamespace(locator=lambda selector: images)))
    service.solve_image.assert_called_once()


@pytest.mark.parametrize('code', ['transport_error', 'ERROR_WRONG_USER_KEY', 'ERROR_ZERO_BALANCE'])
def test_nonrecognition_errors_never_refresh_or_resubmit(monkeypatch, code):
    import runner
    from unittest.mock import AsyncMock
    monkeypatch.setattr(runner, 'captcha_present', AsyncMock(return_value=True))
    handler = SimpleNamespace(remaining_attempts=float('inf'), refresh_challenge=AsyncMock())
    async def fail(page):
        raise CaptchaError(code)
    class CallableHandler:
        remaining_attempts = handler.remaining_attempts
        refresh_challenge = handler.refresh_challenge
        __call__ = AsyncMock(side_effect=fail)
    selected = CallableHandler()
    assert not asyncio.run(runner.wait_captcha(None, runner.Result('query', 'example.com'), 900, selected, runner.CaptchaBudget()))
    selected.__call__.assert_awaited_once()
    handler.refresh_challenge.assert_not_awaited()


def test_answer_timeout_with_no_refresh_button_stops_without_new_submission(monkeypatch):
    import runner
    from unittest.mock import AsyncMock
    monkeypatch.setattr(runner, 'captcha_present', AsyncMock(return_value=True))
    class Handler:
        remaining_attempts = float('inf')
        __call__ = AsyncMock(side_effect=CaptchaError('answer_timeout'))
        refresh_challenge = AsyncMock(side_effect=CaptchaError('refresh_control_unavailable'))
    handler = Handler()
    result = runner.Result('query', 'example.com')
    assert not asyncio.run(runner.wait_captcha(None, result, 900, handler, runner.CaptchaBudget()))
    handler.__call__.assert_awaited_once()
    assert result.detail.endswith('refresh_control_unavailable')


def test_readiness_wait_is_cancelled_by_overall_deadline(monkeypatch):
    import captcha_handler
    from unittest.mock import AsyncMock, Mock
    monkeypatch.setattr(captcha_handler, 'inspect_captcha', AsyncMock(return_value={'state':'changing','candidates':[]}))
    service = SimpleNamespace(solve_image=Mock())
    async def run():
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(.01):
                await PaidSearchCaptchaHandler(service)(None)
    asyncio.run(run())
    service.solve_image.assert_not_called()


@pytest.mark.parametrize('changed', [True, False])
@pytest.mark.parametrize('pending', [True, False])
def test_refresh_requires_changed_loaded_image(monkeypatch, changed, pending):
    import captcha_handler
    from unittest.mock import AsyncMock
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    state = {'state': 'ready', 'candidates': [{'kind': 'silhouette', 'frame_index': 0}]}
    checks = 0
    async def inspect(page):
        nonlocal checks
        checks += 1
        return {**state, 'state': 'not_ready'} if pending and checks == 1 else state
    monkeypatch.setattr(captcha_handler, 'inspect_captcha', inspect)
    calls = 0
    async def sources(*args):
        nonlocal calls
        calls += 1
        return 'new' if changed and calls > 1 else 'old'
    image = SimpleNamespace(evaluate_all=sources)
    button = SimpleNamespace(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True),
                             is_enabled=AsyncMock(return_value=True), click=AsyncMock())
    page = SimpleNamespace(locator=lambda selector: image, get_by_role=lambda *args, **kwargs: button)
    service = SimpleNamespace(solve_image=lambda *args, **kwargs: pytest.fail('Refresh must not submit paid task'))
    handler = PaidSearchCaptchaHandler(service)
    if changed:
        asyncio.run(handler.refresh_challenge(page))
    else:
        with pytest.raises(CaptchaError, match='refresh_not_confirmed'):
            asyncio.run(handler.refresh_challenge(page))
    button.click.assert_awaited_once()


@pytest.mark.parametrize('mode', ['missing', 'exhausted', 'transport', 'unsupported'])
def test_no_failure_enters_human_wait(monkeypatch, mode):
    import runner
    from unittest.mock import AsyncMock
    monkeypatch.setattr(runner, 'captcha_present', AsyncMock(return_value=True))
    sleep = AsyncMock()
    monkeypatch.setattr(runner.asyncio, 'sleep', sleep)
    class Handler:
        remaining_attempts = 0 if mode == 'exhausted' else float('inf')
        async def __call__(self, page):
            raise CaptchaError('transport_error' if mode == 'transport' else 'unsupported_challenge')
    handler = None if mode == 'missing' else Handler()
    result = runner.Result('query', 'example.com')
    assert asyncio.run(runner.wait_captcha(None, result, 900, handler, runner.CaptchaBudget(used=mode == 'exhausted'))) is False
    sleep.assert_not_awaited()
    assert result.status == 'captcha'
    assert 'manual' not in result.detail.lower()


def test_second_captcha_with_finite_remaining_quota_is_automatic(monkeypatch):
    import runner
    from unittest.mock import AsyncMock
    monkeypatch.setattr(runner, 'captcha_present', AsyncMock(side_effect=[True, False, True, False]))
    class Handler:
        remaining_attempts = 2
        calls = 0
        async def __call__(self, page):
            self.calls += 1
            self.remaining_attempts -= 1
            return {'outcome': 'captcha_left'}
    handler = Handler()
    budget = runner.CaptchaBudget()
    result = runner.Result('query', 'example.com')
    assert asyncio.run(runner.wait_captcha(None, result, 900, handler, budget))
    assert asyncio.run(runner.wait_captcha(None, result, 900, handler, budget))
    assert handler.calls == 2
