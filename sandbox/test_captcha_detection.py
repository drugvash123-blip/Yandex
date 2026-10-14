import asyncio
from pathlib import Path
import sys

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Source/bots'))
from captcha_detection import inspect_captcha


def test_observed_layout_classification():
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel='chrome', headless=True, args=['--no-proxy-server'])
            context = await browser.new_context(offline=True)
            try:
                page = await context.new_page()
                image = '<img src="data:image/svg+xml,%3Csvg%20xmlns=%22http://www.w3.org/2000/svg%22%20width=%2230%22%20height=%2220%22/%3E">'
                view = '<div class="AdvancedCaptcha-View">' + image + '</div>'
                controls = '<div class="Textinput"><input value="secret-answer"></div><button class="CaptchaButton_view_action">Check</button>'
                text = '<div class="AdvancedCaptcha">' + view + controls + '</div>'
                cases = [
                    (text, 'ready', 'text'),
                    (text.replace(image, '<img>'), 'not_ready', 'text'),
                    (text.replace('<input value="secret-answer">', '<input disabled>'), 'not_ready', 'text'),
                    ('<div class="AdvancedCaptcha AdvancedCaptcha_silhouette">' + view + '<div class="AdvancedCaptcha-SilhouetteTask">' + image + '</div><button class="CaptchaButton_view_action">Check</button></div>', 'ready', 'silhouette'),
                    ('<div class="CheckboxCaptcha"><button>Checkbox</button></div>', 'not_ready', 'checkbox'),
                    ('<div id="captcha-slider">Slider</div>', 'not_ready', 'slider'),
                    ('<div class="AdvancedCaptcha">Unfamiliar layout</div>', 'not_ready', 'unknown'),
                    (text + text, 'ambiguous', None),
                    ('<div hidden class="CheckboxCaptcha">Hidden</div>' + text, 'ready', 'text'),
                    ('<title>Search</title><p>Results</p>', 'unrecognized', None),
                ]
                for html, state, kind in cases:
                    await page.set_content(html)
                    detection = await inspect_captcha(page)
                    assert detection['state'] == state
                    if kind:
                        assert detection['candidates'][0]['kind'] == kind
                    assert 'secret-answer' not in str(detection)
                await page.set_content('<iframe title="SmartCaptcha advanced"></iframe>')
                frame = page.frames[1]
                await frame.set_content(text)
                detection = await inspect_captcha(page)
                assert detection['state'] == 'ready'
                assert detection['candidates'][0]['frame_index'] == 1
                await page.locator('iframe').evaluate("el=>el.hidden=true")
                assert (await inspect_captcha(page))['state'] == 'unrecognized'
                await page.set_content('<iframe title="SmartCaptcha checkbox"></iframe><iframe title="SmartCaptcha advanced"></iframe>')
                await page.frames[1].set_content('<div class="CheckboxCaptcha">Checkbox</div>')
                await page.frames[2].set_content(text)
                paired = await inspect_captcha(page)
                assert paired['state'] == 'ready'
                assert paired['candidates'][0]['kind'] == 'text'
                assert paired['auxiliary'][0]['kind'] == 'checkbox'
                await page.set_content(text + '<iframe></iframe>')
                await page.frames[1].set_content(text)
                assert (await inspect_captcha(page))['state'] == 'ambiguous'
            finally:
                await context.close()
                await browser.close()
    asyncio.run(run())
