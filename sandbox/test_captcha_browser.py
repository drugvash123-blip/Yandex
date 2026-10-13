import asyncio
from pathlib import Path
import sys

import pytest
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Source/bots'))
from captcha_browser import Challenge, solve_challenge
from captcha_service import CaptchaError


@pytest.mark.parametrize('case', ['text', 'coordinates', 'changed', 'wrong', 'out-of-bounds', 'missing-input'])
def test_local_browser_challenge(case):
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel='chrome', headless=True, args=['--no-proxy-server'])
            context = await browser.new_context(device_scale_factor=2, offline=True)
            try:
                page = await context.new_page()
                await page.set_content('''<div id="image" style="width:100px;height:50px;background:#abc"
                    onclick="window.points=(window.points||0)+1">Example</div>
                    <input id="answer"><button id="submit" onclick="window.submits=(window.submits||0)+1;
                    if(document.querySelector('#answer').value==='correct'||window.points===2)
                    document.querySelector('#success').hidden=false">Submit</button>
                    <div id="success" hidden>Completed</div>''')
                calls = []
                async def fake_solver(image):
                    calls.append(image)
                    if case == 'changed':
                        await page.locator('#image').evaluate("el=>el.style.background='#f00'")
                    if case in ('coordinates', 'out-of-bounds'):
                        return 'coordinate:x=10,y=10;x=50,y=20' if case == 'coordinates' else 'coordinate:x=100,y=10'
                    return 'wrong' if case == 'wrong' else 'correct'
                kind = 'coordinates' if case in ('coordinates', 'out-of-bounds') else 'text'
                spec = Challenge(kind, '#image', '#submit', '#success', '#missing' if case == 'missing-input' else '#answer')
                if case in ('text', 'coordinates'):
                    assert await solve_challenge(page, spec, fake_solver, timeout=5)
                    assert await page.evaluate('window.submits') == 1
                else:
                    with pytest.raises(CaptchaError):
                        await solve_challenge(page, spec, fake_solver, timeout=2)
                    assert await page.evaluate('window.submits||0') == (1 if case == 'wrong' else 0)
                assert len(calls) == (0 if case == 'missing-input' else 1)
            finally:
                await context.close()
                await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('duplicate', [False, True])
def test_challenge_and_success_in_separate_frames(duplicate):
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel='chrome', headless=True, args=['--no-proxy-server'])
            context = await browser.new_context(offline=True)
            try:
                page = await context.new_page()
                await page.set_content('<iframe title="SmartCaptcha checkbox"></iframe><iframe title="SmartCaptcha advanced"></iframe>')
                await page.locator('iframe[title="SmartCaptcha checkbox"]').evaluate('''el=>el.srcdoc=`<input id="js-button" class="CheckboxCaptcha-Button" role="checkbox" aria-checked="false">`''')
                await page.locator('iframe[title="SmartCaptcha advanced"]').evaluate('''el=>el.srcdoc=`<div class="AdvancedCaptcha-View" style="width:100px;height:50px;background:#abc">Image</div>
                  <div class="Textinput"><input class="Textinput-Control"></div>
                  <button class="CaptchaButton_view_action" onclick="if(document.querySelector('input').value==='correct') parent.frames[0].document.querySelector('#js-button').setAttribute('aria-checked','true')">Submit</button>`''')
                await page.frame_locator('iframe[title="SmartCaptcha advanced"]').locator('.Textinput-Control').wait_for()
                if duplicate:
                    await page.locator('iframe[title="SmartCaptcha advanced"]').evaluate('el=>el.after(el.cloneNode(true))')
                calls = []
                async def fake_solver(image):
                    calls.append(image)
                    return 'correct'
                spec = Challenge('text', '.AdvancedCaptcha-View', '.CaptchaButton_view_action',
                                 '#js-button[aria-checked="true"]', '.Textinput-Control',
                                 'iframe[title="SmartCaptcha advanced"]', 'iframe[title="SmartCaptcha checkbox"]')
                if duplicate:
                    with pytest.raises(CaptchaError, match='ambiguous_challenge_frame'):
                        await solve_challenge(page, spec, fake_solver, timeout=5)
                    assert calls == []
                else:
                    assert await solve_challenge(page, spec, fake_solver, timeout=5)
                    assert len(calls) == 1
            finally:
                await context.close()
                await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('outside', [False, True])
def test_capture_includes_instructions_but_clicks_only_image(outside):
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel='chrome', headless=True, args=['--no-proxy-server'])
            context = await browser.new_context(offline=True)
            try:
                page = await context.new_page()
                await page.set_content('''<div id="capture" style="width:100px">
                <div id="picture" style="height:50px;background:#abc" onclick="window.clicks=(window.clicks||0)+1">Image</div>
                <div style="height:50px">Instructions</div></div>
                <button id="submit" onclick="window.submits=1;document.querySelector('#success').hidden=false">Submit</button>
                <div id="success" hidden>Passed</div>''')
                async def solver(image):
                    return 'coordinate:x=10,y=10;x=10,y=70' if outside else 'coordinate:x=10,y=10'
                spec = Challenge('coordinates', '#capture', '#submit', '#success', click_area_selector='#picture')
                if outside:
                    with pytest.raises(CaptchaError, match='coordinates_outside_click_area'):
                        await solve_challenge(page, spec, solver, timeout=5)
                    assert await page.evaluate('(window.clicks||0)+(window.submits||0)') == 0
                else:
                    assert await solve_challenge(page, spec, solver, timeout=5)
                    assert await page.evaluate('window.clicks') == 1
            finally:
                await context.close()
                await browser.close()
    asyncio.run(run())
