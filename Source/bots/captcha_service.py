"""Bounded CapGuru transport. Creating a client sends no request.

Not connected to the browser runner: live use must be explicitly configured.
"""
import base64
import json
import math
import re
import time
from urllib.parse import urlsplit

import requests


class CaptchaError(Exception):
    pass


class CaptchaTimeout(CaptchaError):
    pass


def parse_reply(text):
    """Return (ready, value); never echo an unexpected response or credentials."""
    text = text.strip()
    if text.startswith('{'):
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            raise CaptchaError('invalid_response') from None
        if not isinstance(data, dict) or data.get('status') not in (0, 1) or not isinstance(data.get('request'), str):
            raise CaptchaError('invalid_response')
        if data['status'] == 1 and data['request'].strip():
            return True, data['request']
        text = data['request']
    elif text.startswith('OK|') and text[3:].strip():
        return True, text[3:]
    if text in ('CAPCHA_NOT_READY', 'CAPTCHA_NOT_READY'):
        return False, None
    if re.fullmatch(r'ERROR_[A-Z0-9_]+', text):
        raise CaptchaError(text)
    raise CaptchaError('invalid_response')


def parse_coordinates(answer, width, height):
    if width <= 0 or height <= 0:
        raise CaptchaError('invalid_coordinates')
    prefix = next((prefix for prefix in ('coordinate:', 'coordinates:') if answer.startswith(prefix)), None)
    if prefix is None:
        raise CaptchaError('invalid_coordinates')
    pieces = answer[len(prefix):].split(';')
    if not 1 <= len(pieces) <= 50:
        raise CaptchaError('invalid_coordinates')
    points = []
    for piece in pieces:
        match = re.fullmatch(r'x=(\d+),y=(\d+)', piece.strip())
        if not match:
            raise CaptchaError('invalid_coordinates')
        x, y = map(int, match.groups())
        if not (0 <= x < width and 0 <= y < height):
            raise CaptchaError('coordinates_out_of_bounds')
        points.append((x, y))
    return points


class CaptchaService:
    def __init__(self, key, *, endpoint, deadline=120, result_timeout=60, poll_interval=10,
                 max_polls=12, session=None, clock=time.monotonic, sleep=time.sleep):
        parsed = urlsplit(endpoint)
        if (not key or parsed.scheme != 'https' or not parsed.hostname or
                parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError('An API key and an explicit HTTPS endpoint are required')
        if not all(math.isfinite(value) for value in (deadline, result_timeout, poll_interval)) or deadline <= 0 or result_timeout <= 0 or poll_interval < 5 or max_polls < 1:
            raise ValueError('Invalid waiting limits')
        self.key, self.endpoint = key, endpoint.rstrip('/')
        self.deadline, self.poll_interval, self.max_polls = deadline, poll_interval, max_polls
        self.result_timeout = result_timeout
        self.clock, self.sleep = clock, sleep
        self.session = session if session is not None else requests.Session()
        self.owns_session = session is None
        if self.owns_session:
            self.session.trust_env = False
        self.submitted = False
        self.task_id = None
        self.exchanges = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.owns_session:
            self.session.close()

    def _request(self, path, fields, end):
        remaining = end - self.clock()
        if remaining <= 0:
            raise CaptchaTimeout('deadline_exceeded')
        exchange = {'phase': 'submit' if path == '/in.php' else 'poll'}
        self.exchanges.append(exchange)
        try:
            response = self.session.post(self.endpoint + path, json=fields,
                                         timeout=min(15, remaining / 2), allow_redirects=False)
            exchange['http_status'] = response.status_code
            if response.is_redirect:
                raise CaptchaError('redirect_rejected')
            response.raise_for_status()
        except requests.RequestException as error:
            exchange['error_type'] = type(error).__name__
            # Request exceptions can contain an API key or an entire URL.
            raise CaptchaError('transport_error') from None
        if self.clock() >= end:
            raise CaptchaTimeout('deadline_exceeded')
        return parse_reply(response.text)

    def solve_image(self, image, *, click=None, instructions=None, vernet=None):
        if not isinstance(image, bytes) or not image or len(image) > 5 * 1024 * 1024:
            raise ValueError('Provide an image of at most 5 MiB')
        if self.submitted:
            raise CaptchaError('submission_limit_reached')
        fields = {'key': self.key, 'method': 'base64',
                  'body': base64.b64encode(image).decode('ascii'), 'json': 1}
        if click is not None:
            fields['click'] = click
        if instructions is not None:
            fields['textinstructions'] = instructions
        if vernet is not None:
            fields['vernet'] = vernet
        end = self.clock() + self.deadline
        # An ambiguous network failure can already have created a paid task.
        self.submitted = True
        ready, task_id = self._request('/in.php', fields, end)
        if not ready or not re.fullmatch(r'\d+', task_id):
            raise CaptchaError('invalid_task_id')
        self.task_id = task_id
        import logging
        logging.getLogger(__name__).info('CAPTCHA accepted task id: %s', task_id)
        answer_end = self.clock() + self.result_timeout
        timeout_code = 'answer_timeout' if answer_end <= end else 'deadline_exceeded'
        end = min(end, answer_end)
        for attempt in range(self.max_polls):
            remaining = end - self.clock()
            if remaining <= 0:
                raise CaptchaTimeout(timeout_code)
            self.sleep(min(self.poll_interval, remaining))
            if self.clock() >= end:
                raise CaptchaTimeout(timeout_code)
            try:
                ready, answer = self._request('/res.php', {'key': self.key, 'action': 'get', 'id': task_id, 'json': 1}, end)
            except CaptchaTimeout:
                raise CaptchaTimeout(timeout_code) from None
            except CaptchaError:
                # Reading an existing answer cannot create or charge another task.
                transient = self.exchanges[-1].get('error_type') in ('ConnectionError', 'Timeout', 'ReadTimeout', 'ConnectTimeout')
                if transient and attempt + 1 < self.max_polls:
                    continue
                raise
            if ready:
                return answer
        raise CaptchaTimeout('poll_limit_reached')


class CaptchaQuota:
    """A batch-wide explicit spending limit; each image gets a single-use client."""
    def __init__(self, key, *, endpoint, max_tasks=1, factory=None):
        if max_tasks < 0:
            raise ValueError('max_tasks must be nonnegative; 0 means unlimited')
        self.key, self.endpoint, self.max_tasks = key, endpoint, max_tasks
        self.factory = factory or CaptchaService
        self.clients = []
        import threading
        self.lock = threading.Lock()

    @property
    def remaining(self):
        return self.max_tasks - len(self.clients) if self.max_tasks else float('inf')

    @property
    def exhausted(self):
        return self.remaining == 0

    @property
    def submitted(self):
        return any(client.submitted for client in self.clients)

    @property
    def task_id(self):
        return self.clients[-1].task_id if self.clients else None

    @property
    def exchanges(self):
        return [dict(exchange, attempt=i) for i, client in enumerate(self.clients, 1)
                for exchange in client.exchanges]

    def solve_image(self, image, **options):
        with self.lock:
            if self.exhausted:
                raise CaptchaError('submission_limit_reached')
            client = self.factory(self.key, endpoint=self.endpoint)
            # Consume before transport; an ambiguous failure still spends the limit.
            self.clients.append(client)
        with client:
            return client.solve_image(image, **options)

    def __exit__(self, *args):
        for client in self.clients:
            client.__exit__(*args)
