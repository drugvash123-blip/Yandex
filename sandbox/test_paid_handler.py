import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Source/bots'))
from captcha_handler import PaidSearchCaptchaHandler
from captcha_service import CaptchaError


@pytest.mark.parametrize('kind', ['text', 'silhouette', 'bad-coordinates'])
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
                    return 'answer' if kind == 'text' else ('coordinate:x=10,y=10' if kind == 'silhouette' else 'coordinate:x=10,y=100')
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
