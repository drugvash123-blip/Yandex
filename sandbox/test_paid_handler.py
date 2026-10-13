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


def test_delayed_checkbox_is_clicked_once(monkeypatch):
    import captcha_handler
    import runner
    from unittest.mock import AsyncMock
    states = iter([
        {'state': 'changing', 'candidates': []},
        {'state': 'manual_required', 'candidates': [{'kind': 'checkbox', 'frame_index': 0}]},
        {'state': 'unrecognized', 'candidates': []},
    ])
    async def inspect(page):
        return next(states)
    monkeypatch.setattr(captcha_handler, 'inspect_captcha', inspect)
    monkeypatch.setattr(runner, 'captcha_present', AsyncMock(return_value=False))
    button = SimpleNamespace(count=AsyncMock(return_value=1), click=AsyncMock())
    page = SimpleNamespace(locator=lambda selector: button)
    service = SimpleNamespace(solve_image=lambda *args, **kwargs: pytest.fail('No paid image task for checkbox'))
    outcome = asyncio.run(PaidSearchCaptchaHandler(service, action_pause=0)(page))
    assert outcome['outcome'] == 'challenge_left'
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


def test_definitive_unsolvable_gets_only_one_fresh_attempt(monkeypatch):
    import runner
    from unittest.mock import AsyncMock
    monkeypatch.setattr(runner, 'captcha_present', AsyncMock(return_value=True))
    monkeypatch.setattr(runner.asyncio, 'sleep', AsyncMock())
    class Handler:
        remaining_attempts = float('inf')
        calls = 0
        refresh_challenge = AsyncMock()
        async def __call__(self, page):
            self.calls += 1
            raise CaptchaError('ERROR_CAPTCHA_UNSOLVABLE')
    handler = Handler()
    result = runner.Result('query', 'example.com')
    assert asyncio.run(runner.wait_captcha(None, result, 0, handler, runner.CaptchaBudget())) is False
    assert handler.calls == 2
    handler.refresh_challenge.assert_awaited_once()
    assert result.status == 'captcha'


@pytest.mark.parametrize('changed', [True, False])
def test_refresh_requires_changed_loaded_image(monkeypatch, changed):
    import captcha_handler
    from unittest.mock import AsyncMock
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    state = {'state': 'ready', 'candidates': [{'kind': 'silhouette', 'frame_index': 0}]}
    monkeypatch.setattr(captcha_handler, 'inspect_captcha', AsyncMock(return_value=state))
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
