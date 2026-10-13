import random
import requests
import time
from captcha_service import CaptchaError, CaptchaTimeout, parse_reply

def wait_captcha_response(key, id, timeout=120, max_polls=12):
    """Compatibility wrapper for legacy callers; polling cannot create a task."""
    end = time.monotonic() + timeout
    for _ in range(max_polls):
        remaining = end - time.monotonic()
        if remaining <= 10:
            raise CaptchaTimeout('deadline_exceeded')
        time.sleep(10)
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise CaptchaTimeout('deadline_exceeded')
        try:
            response = requests.get('https://api.cap.guru/res.php',
                                    params={'key': key, 'action': 'get', 'id': id},
                                    timeout=min(15, remaining / 2),
                                    allow_redirects=False)
            if response.is_redirect:
                raise CaptchaError('redirect_rejected')
            response.raise_for_status()
        except requests.RequestException:
            raise CaptchaError('transport_error') from None
        if time.monotonic() >= end:
            raise CaptchaTimeout('deadline_exceeded')
        ready, value = parse_reply(response.text)
        if ready:
            return 'OK|' + value
    raise CaptchaTimeout('poll_limit_reached')

def wait(minTime=1, maxTime=1):
    time.sleep(minTime + (maxTime - minTime) * random.random())
