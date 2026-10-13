"""Offline checks of the original code; no browser or remote services."""
import importlib
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    (tmp_path / "key.txt").write_text("sandbox-placeholder")
    monkeypatch.setattr(sys, "argv", ["sandbox", str(tmp_path)])
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "Source/bots"))
    # Fail closed if a tested path unexpectedly attempts external communication.
    import requests
    import http.client
    def blocked(*args, **kwargs):
        raise AssertionError("Network disabled in offline sandbox")
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", blocked)
    for name in ("bot", "vars", "timeLib", "botAction", "chromeBot"):
        sys.modules.pop(name, None)
    module = importlib.import_module("bot")
    monkeypatch.setattr(module, "wait", lambda *args: None)
    monkeypatch.setattr(module.time, "sleep", lambda *args: None)
    return module


def test_random_boundary(legacy):
    result = legacy.randstr(25)
    assert len(result) == 25 and result.isascii() and result.isalnum()


def test_multipart_contains_fields(legacy):
    body = legacy.generate_from_data([("engine", "yandex")], "test-boundary")
    assert b"name=engine" in body
    assert body.endswith(b"--test-boundary--\r\n")


def test_found_result_records_position(legacy):
    bot = legacy.Bot.__new__(legacy.Bot)
    bot.stats = []
    bot.ip = "sandbox"
    bot.driver = SimpleNamespace(find_elements=lambda *args: [SimpleNamespace(text="other.test"), SimpleNamespace(text="example.test")])
    bot.action = SimpleNamespace(safe_click=lambda element: None)
    bot.scrollToElement = lambda *args: None
    assert bot.check_page("example.test", "test", "1") is True
    assert bot.stats[0]["position"] == "2"


@pytest.mark.xfail(strict=True, reason="Bot.stats is shared between instances")
def test_stats_are_isolated(legacy):
    first, second = legacy.Bot.__new__(legacy.Bot), legacy.Bot.__new__(legacy.Bot)
    first.ip = "first"
    first.edit_stats("test", "example.test", "1", 1)
    assert second.stats == []


@pytest.mark.xfail(strict=True, reason="Negative steps produce an empty range")
def test_scrolls_up(legacy):
    bot = legacy.Bot.__new__(legacy.Bot)
    bot.jsHalfHeight = 400
    bot.isMobile = False
    bot.driver = SimpleNamespace(execute_script=lambda script: 500)
    moves = []
    bot.action = SimpleNamespace(scroll=lambda x, y: moves.append(y))
    bot.scrollToElement(SimpleNamespace(location={"y": 100}), 0)
    assert moves and all(y < 0 for y in moves)


@pytest.mark.xfail(strict=True, reason="Substring matching accepts unrelated domains")
def test_rejects_similar_domain(legacy):
    bot = legacy.Bot.__new__(legacy.Bot)
    bot.ip, bot.stats = "sandbox", []
    bot.driver = SimpleNamespace(find_elements=lambda *args: [SimpleNamespace(text="notexample.test")])
    bot.action = SimpleNamespace(safe_click=lambda element: None)
    bot.scrollToElement = lambda *args: None
    assert bot.check_page("example.test", "test", "1") is False
