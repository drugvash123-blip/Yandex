"""One explicitly configured paid demo test; no task-server access."""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Source/bots'))
from runner import BrowserSession, save_snapshot
from captcha_handler import PaidSearchCaptchaHandler
from captcha_detection import inspect_captcha
from captcha_service import CaptchaService, CaptchaError
from diagnostics import NetworkJournal, safe_error


async def run(args):
    directory = Path('sandbox/artifacts') / ('captcha-live-' + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    proxy = json.loads(args.proxy_config.read_text()) if args.proxy_config else None
    result = {'kind_requested': args.kind, 'status': 'started', 'submission_attempted': False}
    with CaptchaService(args.key_file.read_text().strip(), endpoint='https://api.cap.guru') as service:
        async with BrowserSession(headed=True, proxy=proxy, profile=args.profile) as browser:
            page = browser.context.pages[0] if browser.context.pages else await browser.context.new_page()
            journal = NetworkJournal(browser.context, directory / 'network.jsonl')
            try:
                await page.goto('https://captcha-api.yandex.ru/demo', wait_until='domcontentloaded', timeout=60000)
                checkbox_frame = 'iframe[title="SmartCaptcha checkbox"]'
                button = page.frame_locator(checkbox_frame).locator('#js-button')
                await button.wait_for(state='visible', timeout=30000)
                print('Demo loaded; waiting for manual checkbox' if args.manual_checkbox else 'Demo loaded; pausing before checkbox', flush=True)
                if not args.manual_checkbox:
                    await asyncio.sleep(15)
                    await button.click()
                end = asyncio.get_running_loop().time() + args.challenge_wait
                while True:
                    state = await inspect_captcha(page)
                    (directory / 'diagnosis.json').write_text(json.dumps(state, ensure_ascii=False, indent=2))
                    if state['state'] == 'ready':
                        break
                    if await button.get_attribute('aria-checked') == 'true':
                        result['status'] = 'no_challenge'
                        return
                    if asyncio.get_running_loop().time() > end:
                        raise CaptchaError('challenge_not_ready')
                    await asyncio.sleep(0.5)
                (directory / 'diagnosis.json').write_text(json.dumps(state, ensure_ascii=False, indent=2))
                await save_snapshot(page, directory, 'before')
                actual = state['candidates'][0]['kind']
                result['kind_observed'] = actual
                if actual != args.kind:
                    raise CaptchaError('different_challenge_type')
                print('Detected ' + actual + '; submitting at most one task to CapGuru', flush=True)
                handler = PaidSearchCaptchaHandler(service, outcome_selector='#js-button[aria-checked="true"]',
                                                   outcome_frame_selector=checkbox_frame)
                result['handler_outcome'] = await handler(page)
                (directory / 'after-diagnosis.json').write_text(json.dumps(await inspect_captcha(page), ensure_ascii=False, indent=2))
                checked = await button.get_attribute('aria-checked') == 'true'
                result['status'] = 'passed' if checked else 'not_confirmed'
                await save_snapshot(page, directory, 'after')
            except Exception as error:
                result['status'] = 'failed'
                result['error'] = str(error) if isinstance(error, CaptchaError) else safe_error(error)
                await save_snapshot(page, directory, 'failure')
                for i, frame in enumerate(page.frames):
                    (directory / f'failure-frame-{i}.html').write_text(await frame.content(), encoding='utf-8')
            finally:
                result['submission_attempted'] = service.submitted
                result['exchanges'] = service.exchanges
                result['task_id'] = service.task_id
                (directory / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
                journal.close()
                print(json.dumps({**result, 'artifacts': str(directory.resolve())}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--key-file', type=Path, required=True)
    parser.add_argument('--proxy-config', type=Path)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--manual-checkbox', action='store_true')
    parser.add_argument('--challenge-wait', type=float, default=120)
    parser.add_argument('--kind', choices=('text', 'silhouette'), default='text')
    asyncio.run(run(parser.parse_args()))
