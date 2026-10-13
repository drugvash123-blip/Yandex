from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Source/bots"))
from runner import Result
from task_service import TaskService


@pytest.fixture
def service_server():
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            message = BytesParser(policy=default).parsebytes(
                b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body)
            fields = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True).decode()
                      for part in message.iter_parts()}
            calls.append((self.path, fields))
            if self.server.fail:
                self.send_response(503)
                response = b"Unavailable"
            else:
                self.send_response(200)
                response = b"first | example.com\r\n\r\nsecond\r\n" if self.path.endswith("botsSreetsFile") else b"OK"
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.fail = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}", calls=calls, server=server)
    server.shutdown()
    server.server_close()
    thread.join()


def test_original_task_protocol(service_server):
    with TaskService("test-key", service_server.url) as client:
        tasks = client.fetch()
    assert [(t.query, t.domain) for t in tasks] == [("first", "example.com"), ("second", None)]
    assert service_server.calls == [("/help/botsSreetsFile", {"botsKey": "test-key", "engine": "yandex"})]


def test_statistics_preserve_failures_locally(service_server):
    import json
    results = [Result("found", "example.com", status="found", page=2, position=3),
               Result("absent", "example.com", status="not_found", search_complete=True, completion_reason="page_limit"),
               Result("blocked", "example.com", status="rate_limited"),
               Result("challenge", "example.com", status="captcha")]
    with TaskService("test-key", service_server.url) as client:
        assert client.submit(results, "sandbox-client") is True
    path, fields = service_server.calls[0]
    assert path == "/help/botsStatsFile"
    stats = json.loads(fields["stats"])
    assert len(stats) == 2
    assert stats[0]["page"] == "2" and stats[0]["position"] == "3"
    assert stats[1]["page"] == "Not found"
    assert all(item["ip"] == "sandbox-client" for item in stats)
    assert fields["fileName"] == "sandbox-client"


def test_no_submission_for_captcha(service_server):
    with TaskService("test-key", service_server.url) as client:
        assert client.submit([Result("test", "example.com", status="captcha")], "sandbox") is False
    assert service_server.calls == []


def test_no_submission_for_unproven_absence(service_server):
    with TaskService("test-key", service_server.url) as client:
        assert client.submit([Result("test", "example.com", status="not_found", pages_checked=1)], "sandbox") is False
    assert service_server.calls == []


def test_submission_for_short_confirmed_search(service_server):
    result = Result("short", "example.com", status="not_found", pages_checked=2,
                    search_complete=True, completion_reason="end_of_results")
    with TaskService("test-key", service_server.url) as client:
        assert client.submit([result], "sandbox") is True


def test_http_failure_is_not_empty_tasks(service_server):
    service_server.server.fail = True
    with TaskService("test-key", service_server.url) as client:
        with pytest.raises(requests.HTTPError):
            client.fetch()
        assert client.last_response == {"http_status": 503, "body": "Unavailable"}


def test_service_requires_explicit_key():
    with pytest.raises(ValueError):
        TaskService(None)
