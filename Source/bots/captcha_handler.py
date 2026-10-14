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

    @property
    def remaining_attempts(self):
        return getattr(self.service, 'remaining', 0)

    async def refresh_challenge(self, page):
        """Refresh once and verify a different loaded image before any paid retry."""
        import asyncio
        import hashlib
        from playwright.async_api import Error as BrowserError
        detection = await inspect_captcha(page)
        while detection['state'] != 'ready':
            candidates = detection.get('candidates', [])
            if detection['state'] not in ('changing', 'not_ready') or (candidates and candidates[0]['kind'] not in ('text', 'silhouette')):
                raise CaptchaError('refresh_challenge_not_ready')
            await asyncio.sleep(0.5)
            detection = await inspect_captcha(page)
        candidate = detection['candidates'][0]
        scope = page
        if candidate['frame_index']:
            frames = page.locator('iframe[title="SmartCaptcha advanced"]')
            if await frames.count() != 1:
                raise CaptchaError('unsupported_challenge_frame')
            element = await frames.element_handle()
            if await element.content_frame() != page.frames[candidate['frame_index']]:
                raise CaptchaError('unsupported_challenge_frame')
            scope = page.frame_locator('iframe[title="SmartCaptcha advanced"]')
        images = scope.locator('.AdvancedCaptcha:visible img')
        async def signature():
            raw = await images.evaluate_all('els=>els.map(e=>e.currentSrc||e.src).join("\\n")')
            return hashlib.sha256(raw.encode()).hexdigest()
        before = await signature()
        button = scope.get_by_role('button', name='Обновить задание', exact=True)
        if await button.count() != 1 or not await button.is_visible() or not await button.is_enabled():
            raise CaptchaError('refresh_control_unavailable')
        await asyncio.sleep(15)
        await button.click()
        for _ in range(120):
            try:
                state = await inspect_captcha(page)
                if state['state'] == 'ready' and await signature() != before:
                    return
            except BrowserError:
                pass
            await asyncio.sleep(0.5)
        raise CaptchaError('refresh_not_confirmed')

    async def __call__(self, page):
        import asyncio
        import hashlib
        from runner import captcha_present
        from playwright.async_api import Error as BrowserError

        async def current():
            return await inspect_captcha(page)

        checkbox_clicked = False
        # Images may load after the document and challenge controls appear.
        readiness_checks = 0
        while True:
            detection = await current()
            readiness_checks += 1
            if readiness_checks % 30 == 0:
                import logging
                logging.getLogger(__name__).info('Waiting for CAPTCHA image and controls; state=%s (bounded by task deadline)', detection['state'])
            if detection['state'] == 'not_ready' and detection['candidates'][0]['kind'] == 'slider':
                raise CaptchaError('unsupported_slider_challenge')
            if any(candidate['kind'] in ('text', 'silhouette') for candidate in detection.get('candidates', [])):
                checkbox_clicked = False  # An advanced challenge separates two checkbox stages.
            if not checkbox_clicked and detection['state'] == 'not_ready' and detection['candidates'][0]['kind'] == 'checkbox':
                candidate = detection['candidates'][0]
                scope = page
                if candidate['frame_index']:
                    if candidate.get('frame_title') != 'SmartCaptcha checkbox':
                        raise CaptchaError('unsupported_checkbox_frame')
                    scope = page.frames[candidate['frame_index']]
                button = scope.locator('.CheckboxCaptcha-Button:visible, #js-button:visible')
                if await button.count() != 1:
                    raise CaptchaError('ambiguous_checkbox_control')
                await asyncio.sleep(self.action_pause)
                import logging
                logging.getLogger(__name__).info('CAPTCHA checkbox: clicking (frame=%s)', candidate['frame_index'])
                await button.click()
                logging.getLogger(__name__).info('CAPTCHA checkbox: click completed; waiting for challenge')
                checkbox_clicked = True
            if detection['state'] == 'ready':
                break
            if detection['state'] == 'unrecognized' and not await captcha_present(page):
                return {'outcome': 'captcha_left', 'search_verified': False}
            await asyncio.sleep(0.5)
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
                    if state['state'] == 'not_ready' and len(state.get('candidates', [])) == 1 and state['candidates'][0]['kind'] == 'checkbox':
                        observed['outcome'] = 'checkbox_returned'
                        return
                    if state['state'] == 'unrecognized' and not await captcha_present(page):
                        observed['outcome'] = 'captcha_left'
                        return  # Runner still checks HTTP and the requested query.
                    if state['state'] == 'ready' and await sources() != initial_sources:
                        observed['outcome'] = 'challenge_changed'
                        return  # Runner automatically handles the new challenge.
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
        try:
            await solve_challenge(page, spec, solve, timeout=self.timeout, wait_outcome=outcome,
                                  action_pause=self.action_pause, typing_delay_ms=self.typing_delay_ms)
        except CaptchaError as error:
            if str(error) != 'browser_action_failed':
                raise
            # A browser action can race with replacement of the challenge.
            # Continue only when a new ready image is actually observed.
            for _ in range(40):
                try:
                    state = await current()
                    if state['state'] == 'not_ready' and len(state.get('candidates', [])) == 1 and state['candidates'][0]['kind'] == 'checkbox':
                        return {'outcome': 'checkbox_returned', 'outcome_observed': True, 'search_verified': False}
                    if state['state'] == 'ready' and await sources() != initial_sources:
                        return {'outcome': 'challenge_changed', 'outcome_observed': True, 'search_verified': False}
                    if state['state'] == 'unrecognized' and not await captcha_present(page):
                        return {'outcome': 'captcha_left', 'outcome_observed': True, 'search_verified': False}
                except BrowserError:
                    pass
                await asyncio.sleep(0.5)
            raise
        return {**observed, 'outcome_observed': True, 'search_verified': False}
