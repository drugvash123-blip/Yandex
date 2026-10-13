import asyncio
from pathlib import Path
import sys

import pytest
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Source/bots'))
from runner import RESULT_LINKS, read_result_rows


def test_website_positions_exclude_vertical_cards_and_ads():
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel='chrome', headless=True, args=['--no-proxy-server'])
            context = await browser.new_context(offline=True, java_script_enabled=False)
            try:
                page = await context.new_page()
                await page.set_content('''
                    <div class="serp-item"><h2><a class="OrganicTitle-Link" href="https://first.example/">First</a></h2>
                        <a class="organic__url" href="https://first.example/">Duplicate</a></div>
                    <div class="serp-item serp-item_type_ad"><h2><a href="https://advert.example/">Ad</a></h2></div>
                    <div class="serp-item serp-item__extra-width serp-item_card"><h2><a class="UniSearchHeader-Title" href="https://ya.ru/images/search?text=test">Images</a></h2>
                        <a class="OrganicTitle-Link" href="https://inside-vertical.example/">Thumbnail</a></div>
                    <div class="serp-item"><h2><a href="https://second.example/">Fallback title</a></h2></div>
                    <div class="serp-item"><h2><a class="OrganicTitle-Link" href="https://dms-spb.ru/">Target</a></h2></div>
                    <div class="serp-item"><h2><a class="OrganicTitle-Link" href="https://yandex.ru/company/">Ordinary Yandex website result</a></h2></div>
                ''')
                links = page.locator(RESULT_LINKS)
                rows = await read_result_rows(links)
                assert [r['url'] for r in rows] == ['https://first.example/', 'https://second.example/', 'https://dms-spb.ru/', 'https://yandex.ru/company/']
                assert rows[2]['locatorIndex'] == 3  # Duplicate anchor still occupies index 1.
                assert await links.nth(rows[2]['locatorIndex']).get_attribute('href') == 'https://dms-spb.ru/'
            finally:
                await browser.close()
    asyncio.run(run())


def test_saved_live_page_website_positions():
    # Private artifacts stay untracked. The preceding synthetic regression runs
    # everywhere; this replay additionally verifies the exact observed DOM.
    source = Path(__file__).parent / 'artifacts/94f72e2745544aafa9d8c107f964983d/results-1.html'
    if not source.exists():
        pytest.skip('Private live artifact is unavailable')
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel='chrome', headless=True, args=['--no-proxy-server'])
            context = await browser.new_context(offline=True, java_script_enabled=False)
            try:
                page = await context.new_page()
                await page.set_content(source.read_text())
                links = page.locator(RESULT_LINKS)
                rows = await read_result_rows(links)
                assert len(rows) == 10
                assert not any('/images/search' in row['url'] for row in rows)
                assert rows[7]['url'] == 'https://dms-spb.ru/'
                assert await links.nth(rows[7]['locatorIndex']).get_attribute('href') == 'https://dms-spb.ru/'
            finally:
                await browser.close()
    asyncio.run(run())
