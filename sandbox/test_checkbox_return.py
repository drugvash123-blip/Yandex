import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Source/bots'))
import captcha_handler
import runner
from captcha_service import CaptchaError


def state(kind, ready=False):
    return {'state':'ready' if ready else 'not_ready', 'candidates':[{'kind':kind,'frame_index':0}]}


@pytest.mark.parametrize('browser_error', [False, True])
def test_return_to_checkbox_is_detected_after_submission_or_browser_error(monkeypatch, browser_error):
    monkeypatch.setattr(captcha_handler, 'inspect_captcha', AsyncMock(side_effect=[state('text', True), state('checkbox')]))
    async def solve(page, spec, callback, **kwargs):
        if browser_error:
            raise CaptchaError('browser_action_failed')
        await kwargs['wait_outcome']()
    monkeypatch.setattr(captcha_handler, 'solve_challenge', solve)
    image = SimpleNamespace(evaluate_all=AsyncMock(return_value='image'))
    page = SimpleNamespace(locator=lambda selector: image)
    outcome = asyncio.run(captcha_handler.PaidSearchCaptchaHandler(SimpleNamespace())(page))
    assert outcome['outcome'] == 'checkbox_returned'
    assert outcome['search_verified'] is False


def test_same_checkbox_is_not_clicked_twice_but_new_stage_is(monkeypatch):
    monkeypatch.setattr(captcha_handler, 'inspect_captcha', AsyncMock(side_effect=[
        state('checkbox'),state('checkbox'),state('checkbox'),state('text'),
        state('checkbox'),state('checkbox'),state('text', True)]))
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    monkeypatch.setattr(captcha_handler, 'solve_challenge', AsyncMock())
    button = SimpleNamespace(count=AsyncMock(return_value=1), click=AsyncMock())
    image = SimpleNamespace(evaluate_all=AsyncMock(return_value='image'))
    page = SimpleNamespace(locator=lambda selector: button if 'CheckboxCaptcha' in selector else image)
    asyncio.run(captcha_handler.PaidSearchCaptchaHandler(SimpleNamespace())(page))
    assert button.click.await_count == 2


def test_runner_continues_automatically_when_checkbox_returns(monkeypatch):
    monkeypatch.setattr(runner, 'captcha_present', AsyncMock(side_effect=[True,False]))
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    class Handler:
        remaining_attempts = 2
        __call__ = AsyncMock(side_effect=[{'outcome':'checkbox_returned'},{'outcome':'captcha_left'}])
    handler = Handler()
    result = runner.Result('query','example.com')
    assert asyncio.run(runner.wait_captcha(None,result,900,handler,runner.CaptchaBudget()))
    assert handler.__call__.await_count == 2
    assert [e['outcome'] for e in result.captcha_events if e['event']=='handler_outcome'] == ['checkbox_returned','captcha_left']
