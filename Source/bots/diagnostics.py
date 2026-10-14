"""Passive, redacted diagnostics. Never stores request headers or cookies."""
from datetime import datetime, timezone
import json
import re
import logging
from logging.handlers import RotatingFileHandler
from contextvars import ContextVar

ARTIFACT_MODE = ContextVar('artifact_mode', default='full')


def rotating_log(path, max_bytes=5*1024*1024, backups=3):
    return RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding='utf-8')
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def safe_url(value):
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if ":" in host:
            host = "[" + host + "]"
        if parsed.port:
            host += ":" + str(parsed.port)
        query = urlencode([(key, value) for key, value in parse_qsl(parsed.query)
                           if key in ("text", "p", "lr")])
        return urlunsplit((parsed.scheme, host, parsed.path, query, ""))
    except ValueError:
        return "<invalid URL>"


def safe_error(error):
    codes = re.findall(r"net::ERR_[A-Z_]+", str(error))
    return ", ".join(dict.fromkeys(codes)) or type(error).__name__


def error_diagnostic(error, *, stage, secrets=()):
    """Readable exception without raw traceback source, locals or URL secrets."""
    message = str(error)
    for secret in sorted((str(s) for s in secrets if s), key=len, reverse=True):
        message = message.replace(secret, '<redacted>')
    message = re.sub(r'https?://[^\s<>\"\']+|socks5h?://[^\s<>\"\']+',
                     lambda match: safe_url(match.group()), message)
    message = re.sub(r'(?im)\b(cookie|set-cookie|authorization)\s*:[^\n]+', r'\1: <redacted>', message)
    message = re.sub(r'''(?i)(["']?(?:password|passwd|botsKey|api_key|token|key)["']?\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;]+)''',
                     r'\1<redacted>', message)
    frames = []
    trace = error.__traceback__
    while trace is not None:
        from pathlib import Path
        frames.append({'file': Path(trace.tb_frame.f_code.co_filename).name,
                       'function': trace.tb_frame.f_code.co_name, 'line': trace.tb_lineno})
        trace = trace.tb_next
    return {'stage': stage, 'error_type': type(error).__name__, 'error': safe_error(error),
            'message': message[:4096], 'traceback': frames[-12:]}


class NetworkJournal:
    def __init__(self, context, path):
        self.context = context
        self.handler = rotating_log(path, max_bytes=1024*1024, backups=2)
        self.handler.setFormatter(logging.Formatter('%(message)s'))
        self.handlers = [("request", self.request), ("response", self.response),
                         ("requestfailed", self.failed)]
        for name, callback in self.handlers:
            context.on(name, callback)

    def write(self, event, request, **extra):
        try:
            navigation = request.is_navigation_request()
            if event != "request_failed" and not navigation:
                return
            row = {"time": datetime.now(timezone.utc).isoformat(), "event": event,
                   "url": safe_url(request.url), "method": request.method,
                   "resource_type": request.resource_type, "navigation": navigation, **extra}
            if request.redirected_from:
                row["redirected_from"] = safe_url(request.redirected_from.url)
            self.handler.emit(logging.LogRecord('network', logging.INFO, '', 0, json.dumps(row, ensure_ascii=False), (), None))
        except (OSError, ValueError):
            pass  # Diagnostics must not terminate the browser scenario.

    def request(self, request):
        self.write("request", request)

    def response(self, response):
        self.write("response", response.request, http_status=response.status)

    def failed(self, request):
        self.write("request_failed", request, error=safe_error(request.failure or "unknown"))

    def close(self):
        for name, callback in self.handlers:
            self.context.remove_listener(name, callback)
        self.handler.close()
