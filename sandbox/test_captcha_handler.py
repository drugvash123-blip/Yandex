import asyncio
from pathlib import Path
import sys

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Source/bots'))
from captcha_handler import TextCaptchaHandler
from captcha_service import CaptchaError


def test_selection_before_solver_and_separate_confirmation_frame():
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel='chrome', headless=True, args=['--no-proxy-server'])
            context = await browser.new_context(offline=True)
            try:
                page = await context.new_page()
                text = '''<div class="AdvancedCaptcha"><div class="AdvancedCaptcha-View">
                    <img src="data:image/svg+xml,%3Csvg%20xmlns=%22http://www.w3.org/2000/svg%22%20width=%2230%22%20height=%2220%22/%3E"></div>
                    <div class="Textinput"><input></div><button class="CaptchaButton_view_action"
                    onclick="document.querySelector('#done').hidden=false">Check</button></div>
                    <p id="error" hidden>Error</p><p id="done" hidden>Done</p>'''
                calls = []
                async def solver(image):
                    calls.append(image)
                    return 'local answer'
                handler = TextCaptchaHandler(solver, outcome_selector='#error, #done', timeout=2)
                await page.set_content(text)
                result = await handler(page)
                assert result == {'outcome_observed': True, 'search_verified': False}
                assert len(calls) == 1
                assert await page.locator('input').input_value() == 'local answer'

                for html in [text.replace('<img src=', '<img data-src='),
                             text.replace('class="AdvancedCaptcha"', 'class="AdvancedCaptcha AdvancedCaptcha_silhouette"'),
                             text + text, '<div class="CheckboxCaptcha">Checkbox</div>',
                             '<div id="captcha-slider">Slider</div>']:
                    calls.clear()
                    await page.set_content(html)
                    try:
                        await handler(page)
                    except CaptchaError:
                        pass
                    else:
                        raise AssertionError('Unsupported layout must stop before solver')
                    assert not calls

                await page.set_content('<iframe title="SmartCaptcha checkbox"></iframe><iframe title="SmartCaptcha advanced"></iframe>')
                await page.frames[1].set_content('<input id="js-button" aria-checked="false">')
                await page.frames[2].set_content(text.replace("document.querySelector('#done').hidden=false", "parent.frames[0].document.querySelector('#js-button').setAttribute('aria-checked','true')"))
                handler = TextCaptchaHandler(solver, outcome_selector='#js-button[aria-checked="true"]',
                                            outcome_frame_selector='iframe[title="SmartCaptcha checkbox"]', timeout=2)
                calls.clear()
                await handler(page)
                assert len(calls) == 1

                # A visually identical layout in an unknown frame is not accepted.
                await page.locator('iframe[title="SmartCaptcha advanced"]').evaluate("el=>el.title='Other frame'")
                calls.clear()
                try:
                    await handler(page)
                except CaptchaError as error:
                    assert str(error) == 'unsupported_challenge_frame'
                else:
                    raise AssertionError('Unknown frame must remain manual')
                assert not calls
            finally:
                await context.close()
                await browser.close()
    asyncio.run(run())
