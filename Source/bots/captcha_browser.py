"""Browser actions for an explicitly described challenge; no service calls by default."""
from dataclasses import dataclass
import asyncio
import hashlib

from playwright.async_api import Error as BrowserError
from captcha_service import CaptchaError, parse_coordinates


@dataclass(frozen=True)
class Challenge:
    kind: str
    image_selector: str
    submit_selector: str
    success_selector: str
    input_selector: str | None = None
    frame_selector: str | None = None
    success_frame_selector: str | None = None
    click_area_selector: str | None = None


async def solve_challenge(page, challenge, solve, *, timeout=30):
    """solve is an injected async callback(image_bytes); caller owns the paid budget.

    Explicit selectors make this testable without pretending that every current
    Yandex CAPTCHA layout is supported. No reloads or repeated submissions.
    """
    if challenge.kind not in ('text', 'coordinates') or not challenge.success_selector:
        raise CaptchaError('unsupported_challenge')
    try:
        async with asyncio.timeout(timeout):
            scope = page
            success_scope = page
            if challenge.frame_selector:
                if await page.locator(challenge.frame_selector).count() != 1:
                    raise CaptchaError('ambiguous_challenge_frame')
                scope = page.frame_locator(challenge.frame_selector)
            if challenge.success_frame_selector:
                if await page.locator(challenge.success_frame_selector).count() != 1:
                    raise CaptchaError('ambiguous_success_frame')
                success_scope = page.frame_locator(challenge.success_frame_selector)
            image = scope.locator(challenge.image_selector)
            submit = scope.locator(challenge.submit_selector)
            success = success_scope.locator(challenge.success_selector)
            if await image.count() != 1 or await submit.count() != 1:
                raise CaptchaError('ambiguous_controls')
            if not await image.is_visible() or not await submit.is_visible() or not await submit.is_enabled():
                raise CaptchaError('controls_unavailable')
            if await success.count() and await success.first.is_visible():
                raise CaptchaError('success_already_visible')
            field = None
            if challenge.kind == 'text':
                if not challenge.input_selector:
                    raise CaptchaError('missing_input')
                field = scope.locator(challenge.input_selector)
                if await field.count() != 1 or not await field.is_visible() or not await field.is_editable():
                    raise CaptchaError('input_unavailable')
            before = await image.screenshot(scale='css', animations='disabled')
            box = await image.bounding_box()
            answer = await solve(before)
            after = await image.screenshot(scale='css', animations='disabled')
            if hashlib.sha256(before).digest() != hashlib.sha256(after).digest():
                raise CaptchaError('challenge_changed')
            new_box = await image.bounding_box()
            if box is None or new_box is None or (box['width'], box['height']) != (new_box['width'], new_box['height']):
                raise CaptchaError('image_size_changed')
            if not isinstance(answer, str):
                raise CaptchaError('invalid_answer')
            if challenge.kind == 'text':
                if not answer.strip() or len(answer) > 120 or '\n' in answer or '\r' in answer:
                    raise CaptchaError('invalid_text_answer')
                await field.fill(answer)
            else:
                points = parse_coordinates(answer, box['width'], box['height'])
                if challenge.click_area_selector:
                    area = scope.locator(challenge.click_area_selector)
                    if await area.count() != 1 or not await area.is_visible():
                        raise CaptchaError('click_area_unavailable')
                    area_box = await area.bounding_box()
                    if area_box is None:
                        raise CaptchaError('click_area_unavailable')
                    # Validate every point before performing any click. The screenshot
                    # may contain instructions and controls outside the allowed image.
                    if any(not (area_box['x'] <= new_box['x'] + x < area_box['x'] + area_box['width'] and
                                area_box['y'] <= new_box['y'] + y < area_box['y'] + area_box['height']) for x, y in points):
                        raise CaptchaError('coordinates_outside_click_area')
                for x, y in points:
                    await image.click(position={'x': x, 'y': y})
            await submit.click()
            await success.first.wait_for(state='visible')
            return True
    except TimeoutError:
        raise CaptchaError('browser_deadline_exceeded') from None
    except BrowserError:
        raise CaptchaError('browser_action_failed') from None
