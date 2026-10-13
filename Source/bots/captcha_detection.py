"""Read-only classification of observed Yandex challenge layouts.

No answers, cookies, tokens or image URLs are collected. A detected type is
not proof that a solver supports it or that search access has been restored.
"""
from playwright.async_api import Error as BrowserError


async def inspect_captcha(page):
    candidates = []
    for index, frame in enumerate(page.frames):
        try:
            if frame != page.main_frame:
                element = await frame.frame_element()
                if not await element.is_visible():
                    continue
            state = await frame.evaluate('''() => {
                const visible = el => !!el && el.getClientRects().length > 0 &&
                    getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
                const roots = [...document.querySelectorAll('.AdvancedCaptcha, .CheckboxCaptcha, #captcha-slider')].filter(visible);
                return roots.map(root => {
                    const items = selector => [...root.querySelectorAll(selector)].filter(visible);
                    let kind = 'unknown';
                    if(root.matches('#captcha-slider')) kind = 'slider';
                    else if(root.matches('.CheckboxCaptcha')) kind = 'checkbox';
                    else if(root.matches('.AdvancedCaptcha_silhouette') || items('.AdvancedCaptcha-SilhouetteTask').length) kind = 'silhouette';
                    else if(items('.Textinput input').length === 1) kind = 'text';
                    const images = items('img');
                    const field = items('.Textinput input');
                    const submit = items('.CaptchaButton_view_action');
                    const views = items('.AdvancedCaptcha-View');
                    const imageReady = images.length > 0 && images.every(img => img.complete && img.naturalWidth > 0);
                    const controlsReady = submit.length === 1 && !submit[0].disabled &&
                        (kind !== 'text' || (field.length === 1 && !field[0].disabled && !field[0].readOnly));
                    return {kind, images:images.length, image_ready:imageReady,
                        ready:(kind === 'text' || kind === 'silhouette') && imageReady && controlsReady && views.length === 1};
                });
            }''')
            candidates.extend({'frame_index': index, **item} for item in state)
        except BrowserError:
            return {'state': 'changing', 'candidates': candidates}
    if not candidates:
        return {'state': 'unrecognized', 'candidates': []}
    if len(candidates) != 1:
        return {'state': 'ambiguous', 'candidates': candidates}
    candidate = candidates[0]
    return {'state': 'ready' if candidate['ready'] else 'manual_required', 'candidates': candidates}
