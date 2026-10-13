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
