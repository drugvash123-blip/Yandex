"""Select only the observed text layout; callers provide an outcome criterion.

No service is constructed here. The async solver is injected and the runner
owns the one-attempt budget. An outcome is not proof of valid search results.
"""
from captcha_browser import Challenge, solve_challenge
from captcha_detection import inspect_captcha
from captcha_service import CaptchaError


class TextCaptchaHandler:
    def __init__(self, solve, *, outcome_selector, outcome_frame_selector=None, timeout=30):
        if not callable(solve) or not outcome_selector or timeout <= 0:
            raise ValueError('Provide a solver, an explicit outcome selector and a positive timeout')
        self.solve = solve
        self.outcome_selector = outcome_selector
        self.outcome_frame_selector = outcome_frame_selector
        self.timeout = timeout

    async def __call__(self, page):
        detection = await inspect_captcha(page)
        if detection['state'] != 'ready':
            raise CaptchaError('challenge_not_ready')
        candidate = detection['candidates'][0]
        if candidate['kind'] != 'text':
            raise CaptchaError('unsupported_challenge')
        frame_selector = None
        if candidate['frame_index']:
            # This title was observed on the demo. Do not infer frame selectors
            # from arbitrary URLs or assume all advanced frames are equivalent.
            frame_selector = 'iframe[title="SmartCaptcha advanced"]'
            frames = page.locator(frame_selector)
            if await frames.count() != 1 or not await frames.is_visible():
                raise CaptchaError('unsupported_challenge_frame')
            element = await frames.element_handle()
            if await element.content_frame() != page.frames[candidate['frame_index']]:
                raise CaptchaError('unsupported_challenge_frame')
        root = '.AdvancedCaptcha:visible'
        spec = Challenge('text', root + ' .AdvancedCaptcha-View:visible',
                         root + ' .CaptchaButton_view_action:visible',
                         self.outcome_selector,
                         input_selector=root + ' .Textinput input:visible',
                         frame_selector=frame_selector,
                         success_frame_selector=self.outcome_frame_selector)
        await solve_challenge(page, spec, self.solve, timeout=self.timeout)
        return {'outcome_observed': True, 'search_verified': False}


class PaidSearchCaptchaHandler:
    """Explicit opt-in; a single service instance owns the paid submission budget."""
    def __init__(self, service, *, timeout=150, outcome_selector=None, outcome_frame_selector=None,
                 allowed_kinds=('text', 'silhouette'), action_pause=2, typing_delay_ms=200):
        self.service = service
        self.allowed_kinds = allowed_kinds
        self.action_pause, self.typing_delay_ms = action_pause, typing_delay_ms
        self.timeout = timeout
        self.outcome_selector = outcome_selector
        self.outcome_frame_selector = outcome_frame_selector

    async def __call__(self, page):
        import asyncio
        import hashlib
        from runner import captcha_present
        from playwright.async_api import Error as BrowserError

        async def current():
            return await inspect_captcha(page)

        detection = await current()
        if detection['state'] == 'manual_required' and detection['candidates'][0]['kind'] == 'checkbox':
            button = page.locator('.CheckboxCaptcha-Button:visible')
            if detection['candidates'][0]['frame_index'] or await button.count() != 1:
                raise CaptchaError('manual_checkbox_required')
            await button.click()
        # Images may load after the document and challenge controls appear.
        for _ in range(120):
            detection = await current()
            if detection['state'] == 'ready':
                break
            if detection['state'] == 'unrecognized' and not await captcha_present(page):
                return {'outcome': 'challenge_left', 'search_verified': False}
            await asyncio.sleep(0.5)
        if detection['state'] != 'ready':
            raise CaptchaError('challenge_not_ready')
        candidate = detection['candidates'][0]
        kind = candidate['kind']
        if kind not in ('text', 'silhouette') or kind not in self.allowed_kinds:
            raise CaptchaError('unsupported_challenge')
        frame_selector = None
        scope = page
        if candidate['frame_index']:
            frame_selector = 'iframe[title="SmartCaptcha advanced"]'
            elements = page.locator(frame_selector)
            if await elements.count() != 1 or not await elements.is_visible():
                raise CaptchaError('unsupported_challenge_frame')
            element = await elements.element_handle()
            if await element.content_frame() != page.frames[candidate['frame_index']]:
                raise CaptchaError('unsupported_challenge_frame')
            scope = page.frame_locator(frame_selector)
        root = '.AdvancedCaptcha:visible'
        async def sources():
            raw = await scope.locator(root + ' img').evaluate_all('els=>els.map(e=>e.currentSrc||e.src).join("\\n")')
            return hashlib.sha256(raw.encode()).hexdigest()
        initial_sources = await sources()
        outcome_scope = page
        if self.outcome_frame_selector:
            if await page.locator(self.outcome_frame_selector).count() != 1:
                raise CaptchaError('ambiguous_success_frame')
            outcome_scope = page.frame_locator(self.outcome_frame_selector)
        observed = {'outcome': 'unknown'}
        async def outcome():
            while True:
                try:
                    if self.outcome_selector and await outcome_scope.locator(self.outcome_selector).is_visible():
                        observed['outcome'] = 'explicit_confirmation'
                        return
                    state = await current()
                    if state['state'] == 'unrecognized' and not await captcha_present(page):
                        observed['outcome'] = 'captcha_left'
                        return  # Runner still checks HTTP and the requested query.
                    if state['state'] == 'ready' and await sources() != initial_sources:
                        observed['outcome'] = 'challenge_changed'
                        return  # New task: budget remains spent, manual fallback.
                except BrowserError:
                    pass  # Navigation can replace the document mid-observation.
                await asyncio.sleep(0.25)
        async def solve(image):
            options = {'vernet': 18} if kind == 'text' else {'click': 'oth', 'instructions': 'yandex'}
            try:
                return await asyncio.to_thread(self.service.solve_image, image, **options)
            finally:
                import logging
                logging.getLogger(__name__).info('CAPTCHA API exchanges: %s', getattr(self.service, 'exchanges', []))
        spec = Challenge('text' if kind == 'text' else 'coordinates',
                         root + ' .AdvancedCaptcha-View:visible' if kind == 'text' else root,
                         root + ' .CaptchaButton_view_action:visible', '',
                         input_selector=root + ' .Textinput input:visible' if kind == 'text' else None,
                         frame_selector=frame_selector,
                         click_area_selector=root + ' .AdvancedCaptcha-View:visible' if kind == 'silhouette' else None)
        await solve_challenge(page, spec, solve, timeout=self.timeout, wait_outcome=outcome,
                              action_pause=self.action_pause, typing_delay_ms=self.typing_delay_ms)
        return {**observed, 'outcome_observed': True, 'search_verified': False}
