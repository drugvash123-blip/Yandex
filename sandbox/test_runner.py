import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit, urlencode

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Source/bots"))
from runner import Task, matches_domain, run_task
from runner import Result, search, BrowserSession
from runner import completed_search
from unittest.mock import AsyncMock, Mock
from playwright.async_api import TimeoutError as BrowserTimeoutError


@pytest.fixture
def local_site():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.server.requests.append(self.path)
            args = parse_qs(urlsplit(self.path).query)
            query = args.get("text", [""])[0]
            index = args.get("p", ["0"])[0]
            status = 200
            if query == "slow-submit":
                time.sleep(32)
            if self.path == "/deferred-home":
                html = '''<title>Home</title><form action="/search" onsubmit="this.elements.dcl.value=performance.getEntriesByType('navigation')[0].domContentLoadedEventEnd">
                <input name="text"><input type="hidden" name="dcl"><button>Search</button></form>
                <script defer src="/slow-asset.js"></script>'''
            elif self.path == "/slow-asset.js":
                time.sleep(3)
                html = "/* delayed resource */"
            elif self.path == "/":
                html = '<title>Home</title><form action="/search"><input name="text"><button>Search</button></form>'
            elif query == "rate-limit":
                status = 429
                html = '<title>429</title><p>Too many requests</p>'
            elif query == "rate-limit-recovers" and "recovered" not in args:
                status = 429
                html = '<title>429</title><script>setTimeout(()=>location.search+="&recovered=1",700)</script>'
            elif query == "rate-limit-twice" and index == "0" and "recovered" not in args:
                status = 429
                html = '<title>429</title><script>setTimeout(()=>location.search+="&recovered=1",700)</script>'
            elif query == "rate-limit-twice" and index == "1":
                status = 429
                html = '<title>429</title>'
            elif query == "pagination-captcha" and index == "1":
                html = '<title>Вы не робот?</title><div class="CheckboxCaptcha"></div>'
            elif query == "http-error":
                status = 503
                html = '<title>Unavailable</title>'
            elif query == "captcha":
                html = '<title>Вы не робот?</title><div class="CheckboxCaptcha"></div>'
            elif query == "captcha-cleared":
                target = f"http://127.0.0.1:{self.server.server_port}/target"
                html = f'''<title>Вы не робот?</title><div class="CheckboxCaptcha"></div>
                <script>setTimeout(() => {{document.title='Search'; document.body.innerHTML='<div class="serp-item"><h2><a class="OrganicTitle-Link" href="{target}">Result</a></h2></div>'; }}, 700)</script>'''
            elif self.path.startswith("/target"):
                html = "<title>Target</title><h1>Destination reached</h1>"
                if self.path == "/target-error":
                    status = 503
                if self.path == "/target-deferred":
                    html += '<script defer src="/slow-asset.js"></script>'
            else:
                target = f"http://127.0.0.1:{self.server.server_port}/target?item={index}"
                if query in ("second", "more", "rate-limit-twice", "pagination-captcha") and index == "0":
                    target = "https://other.invalid/"
                target_attr = '' if query == 'same-tab' else 'target="_blank"'
                if query == "target-error":
                    target = f"http://127.0.0.1:{self.server.server_port}/target-error"
                if query == "target-deferred":
                    target = f"http://127.0.0.1:{self.server.server_port}/target-deferred"
                html = f'<title>Search</title><div class="serp-item"><h2><a class="OrganicTitle-Link" href="{target}" {target_attr}>Result</a><a href="{target}">Duplicate</a></h2></div>'
                if query == "late-popup":
                    html = f'''<title>Search</title><div class="serp-item"><h2><a class="OrganicTitle-Link" href="{target}" onclick="event.preventDefault();setTimeout(()=>window.open(this.href),2000)">Result</a></h2></div>'''
                if index == "0":
                    if query == "more":
                        actual = f"http://127.0.0.1:{self.server.server_port}/target"
                        html += f'''<button onclick="this.insertAdjacentHTML('beforebegin', '&lt;div class=&quot;serp-item&quot;&gt;&lt;h2&gt;&lt;a href=&quot;{actual}&quot;&gt;Target&lt;/a&gt;&lt;/h2&gt;&lt;/div&gt;');this.remove()">Показать ещё</button>'''
                    else:
                        next_url = "/search?" + urlencode({"text": query, "p": 1})
                        html += f'<a href="{next_url}">2</a>'
            if query == "short" and index == "1":
                html += '<nav class="Pager"><a href="/search?text=short">1</a><span aria-current="page">2</span><button rel="next" disabled>Next</button></nav>'
            if query == "single":
                html = html.split('<a href="/search?')[0]
                html += '<nav class="Pager"><span aria-current="page">1</span><button rel="next" disabled>Next</button></nav>'
            if query == "missing-pager":
                html = html.split('<a href="/search?')[0]
            if query == "repeated" and index == "1":
                html = html.replace('/target?item=1', '/target?item=0')
            if query == "wrong-query" and index == "0":
                html = html.replace('text=wrong-query', 'text=other-query')
            if query == "five" and 0 < int(index) < 4:
                html += f'<a href="/search?text=five&p={int(index)+1}">{int(index)+2}</a>'
            if query == "delayed" and index == "1":
                html = html.replace('/target?item=1', '/target?item=0')
                html += '<script>setTimeout(()=>document.querySelector(".OrganicTitle-Link").href="/target?item=1",1000)</script>'
            body = ("<!doctype html><meta charset=utf-8>" + html).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/javascript" if self.path == "/slow-asset.js" else "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}/", requests=server.requests)
    server.shutdown()
    server.server_close()
    thread.join()


def test_hostname_boundaries():
    assert matches_domain("https://www.example.com/a", "example.com")
    assert not matches_domain("https://notexample.com/", "example.com")
    assert not matches_domain("https://example.com.other.org/", "example.com")


@pytest.mark.parametrize("query,status,pages,reason", [
    ("short", "not_found", 2, "end_of_results"),
    ("single", "not_found", 1, "end_of_results"),
    ("five", "not_found", 5, "page_limit"),
    ("missing-pager", "incomplete", 1, None),
    ("repeated", "pagination_stalled", 1, None),
    ("wrong-query", "incomplete", 1, None),
    ("delayed", "not_found", 2, "page_limit"),
])
def test_page_coverage(local_site, tmp_path, query, status, pages, reason):
    limit = 2 if query == "delayed" else 5
    result = asyncio.run(run_task(Task(query, "absent.invalid"), base_url=local_site.url,
                                 max_pages=limit, deadline=35, output=tmp_path, typing_delay_ms=0))
    assert result.status == status, result
    assert result.pages_checked == pages
    assert len(result.page_checks) == pages
    assert result.completion_reason == reason
    assert result.search_complete == (status == "not_found")
    assert completed_search(result, limit) == (status == "not_found")


def test_task_validation():
    with pytest.raises(ValueError):
        Task(" ", "example.com")


@pytest.mark.parametrize("status,pages,expected", [("found", 2, True), ("not_found", 5, True),
                                                  ("not_found", 2, False), ("captcha", 5, False),
                                                  ("rate_limited", 5, False)])
def test_completed_search(status, pages, expected):
    assert completed_search(Result("test", "example.com", status=status, pages_checked=pages, search_complete=pages == 5, completion_reason="page_limit" if pages == 5 else None), 5) is expected


@pytest.mark.parametrize("query,domain,click,status,expected_page", [
    ("first", "127.0.0.1", True, "found", 1),
    ("second", "127.0.0.1", True, "found", 2),
    ("same-tab", "127.0.0.1", True, "found", 1),
    ("more", "127.0.0.1", True, "found", 2),
    ("random", None, True, "found", 1),
    ("absent", "example.invalid", False, "not_found", None),
    ("captcha", "127.0.0.1", False, "captcha", None),
    ("rate-limit", "127.0.0.1", False, "rate_limited", None),
    ("http-error", "127.0.0.1", False, "http_error", None),
])
def test_browser_scenarios(local_site, tmp_path, query, domain, click, status, expected_page):
    result = asyncio.run(run_task(Task(query, domain), base_url=local_site.url,
                                 max_pages=2, deadline=30, click=click, output=tmp_path, typing_delay_ms=0))
    assert result.status == status, result
    assert result.page == expected_page
    assert local_site.requests[0] == "/"
    submitted = [r for r in local_site.requests if r.startswith("/search?")]
    assert parse_qs(urlsplit(submitted[0]).query)["text"] == [query]
    assert "p" not in parse_qs(urlsplit(submitted[0]).query)
    if expected_page == 2:
        assert result.pages_checked == 2
    if click:
        assert "/target" in result.visited_url
    assert (Path(result.artifacts) / "result.json").exists()
    assert (Path(result.artifacts) / "trace.zip").exists()


def test_deadline(tmp_path):
    result = asyncio.run(run_task(Task("test", "example.com"), deadline=0.001, output=tmp_path, typing_delay_ms=0))
    assert result.status == "timeout"


@pytest.mark.parametrize("query,wait,status", [("captcha", 0.1, "captcha"), ("captcha-cleared", 3, "found")])
def test_captcha_wait(local_site, tmp_path, query, wait, status):
    result = asyncio.run(run_task(Task(query, "127.0.0.1"), base_url=local_site.url,
                                 deadline=20, captcha_wait=wait, output=tmp_path, typing_delay_ms=0))
    assert result.status == status, result


@pytest.mark.parametrize("query,status", [("rate-limit-recovers", "found"), ("rate-limit", "rate_limited")])
def test_rate_limit_passive_recovery(local_site, tmp_path, query, status):
    result = asyncio.run(run_task(Task(query, "127.0.0.1"), base_url=local_site.url,
                                 max_pages=1, deadline=20, captcha_wait=3,
                                 output=tmp_path, typing_delay_ms=0))
    assert result.status == status, result
    submitted = [r for r in local_site.requests if r.startswith("/search?")]
    assert len(submitted) == (2 if status == "found" else 1)
    if status == "found":
        assert result.http_status == 200
        assert "/target" in result.visited_url
        assert any("Страница восстановилась" in a["description"] for a in result.actions)


@pytest.mark.parametrize("query,status,count", [("rate-limit-twice", "rate_limited", 3), ("pagination-captcha", "captcha", 2)])
def test_pagination_stops_on_repeated_challenge(local_site, tmp_path, query, status, count):
    result = asyncio.run(run_task(Task(query, "127.0.0.1"), base_url=local_site.url,
                                 max_pages=3, deadline=20, captcha_wait=3,
                                 output=tmp_path, typing_delay_ms=0))
    assert result.status == status, result
    assert result.pages_checked == 1
    assert len([r for r in local_site.requests if r.startswith("/search?")]) == count
    assert sum("HTTP 429: ожидание" in a["description"] for a in result.actions) <= 1


def test_results_pause_precedes_pagination(local_site, tmp_path, monkeypatch):
    original_sleep = asyncio.sleep
    waits = []
    async def fast_sleep(seconds):
        waits.append(seconds)
        await original_sleep(min(seconds, 0.05))
    monkeypatch.setattr("runner.asyncio.sleep", fast_sleep)
    result = asyncio.run(run_task(Task("second", "127.0.0.1"), base_url=local_site.url,
                                 max_pages=2, deadline=20, output=tmp_path,
                                 typing_delay_ms=0, action_pause=5, visit_seconds=0))
    assert result.status == "found", result
    descriptions = [a["description"] for a in result.actions]
    assert descriptions.index("Ожидание перед просмотром результатов 15 с") < descriptions.index("Прокрутка к элементу перехода на страницу/порцию 2")
    assert waits.count(15) == 2


@pytest.mark.parametrize("query,status", [("late-popup", "found"), ("target-deferred", "found"), ("target-error", "navigation_failed")])
def test_destination_readiness(local_site, tmp_path, query, status):
    result = asyncio.run(run_task(Task(query, "127.0.0.1"), base_url=local_site.url,
                                 max_pages=1, deadline=20, output=tmp_path, typing_delay_ms=0))
    assert result.status == status, result


def test_navigation_timeout_with_captcha(monkeypatch):
    import runner
    page = Mock()
    page.goto = AsyncMock(side_effect=BrowserTimeoutError("navigation timeout"))
    page.locator.return_value.wait_for = AsyncMock()
    monkeypatch.setattr(runner, "captcha_present", AsyncMock(return_value=True))
    result = Result("test", "example.com")
    asyncio.run(search(page, Task("test", "example.com"), result, "https://ya.ru/search/", 1, False))
    assert result.status == "captcha"


def test_navigation_timeout_without_captcha(monkeypatch):
    import runner
    page = Mock()
    page.goto = AsyncMock(side_effect=BrowserTimeoutError("navigation timeout"))
    monkeypatch.setattr(runner, "captcha_present", AsyncMock(return_value=False))
    result = Result("test", "example.com")
    asyncio.run(search(page, Task("test", "example.com"), result, "https://ya.ru/search/", 1, False))
    assert result.status == "navigation_timeout"


def test_batch_reuses_browser_and_cookies(local_site, tmp_path):
    async def batch():
        async with BrowserSession() as session:
            await session.context.add_cookies([{"name": "sandbox_session", "value": "retained", "url": local_site.url}])
            first = await run_task(Task("first", "127.0.0.1"), base_url=local_site.url, session=session, output=tmp_path, typing_delay_ms=0)
            assert first.status == "found"
            assert len(session.context.pages) == 1
            page = session.context.pages[0]
            second = await run_task(Task("second", "127.0.0.1"), base_url=local_site.url, session=session, output=tmp_path, typing_delay_ms=0)
            assert second.status == "found" and second.page == 2
            assert session.context.pages[0] is page
            assert len(session.context.pages) == 1
            assert any(c["name"] == "sandbox_session" and c["value"] == "retained" for c in await session.context.cookies())
        assert not session.browser.is_connected()
    asyncio.run(batch())


def test_slow_long_query_is_submitted_completely(local_site, tmp_path):
    query = "мебель из массива сосны"
    result = asyncio.run(run_task(Task(query, "127.0.0.1"), base_url=local_site.url,
                                 typing_delay_ms=500, deadline=30, output=tmp_path))
    assert result.status == "found", result
    searches = [path for path in local_site.requests if path.startswith("/search?")]
    assert parse_qs(urlsplit(searches[0]).query)["text"] == [query]


def test_action_pauses_and_visit_are_recorded(local_site, tmp_path):
    from datetime import datetime
    result = asyncio.run(run_task(Task("first", "127.0.0.1"), base_url=local_site.url,
                                 typing_delay_ms=0, action_pause=0.1, visit_seconds=0.2,
                                 deadline=20, output=tmp_path))
    assert result.status == "found", result
    actions = result.actions
    for index, event in enumerate(actions):
        if event["description"] == "Пауза 0.1 с":
            elapsed = datetime.fromisoformat(actions[index + 1]["time"]) - datetime.fromisoformat(event["time"])
            assert elapsed.total_seconds() >= 0.09
    assert any(event["description"] == "Ожидание на сайте 0.2 с" for event in actions)
    assert (Path(result.artifacts) / "results-1.json").exists()


def test_slow_form_navigation_is_not_reported_as_failed(local_site, tmp_path):
    result = asyncio.run(run_task(Task("slow-submit", "127.0.0.1"), base_url=local_site.url,
                                 typing_delay_ms=0, deadline=50, output=tmp_path))
    assert result.status == "found", result


def test_homepage_form_can_be_used_before_deferred_resources_finish(local_site, tmp_path):
    result = asyncio.run(run_task(Task("first", "127.0.0.1"), base_url=local_site.url + "deferred-home",
                                 typing_delay_ms=0, deadline=20, output=tmp_path))
    assert result.status == "found", result
    path = next(p for p in local_site.requests if p.startswith("/search?"))
    assert float(parse_qs(urlsplit(path).query)["dcl"][0]) == 0


def test_profile_persists_cookies_and_local_storage_across_launches(local_site, tmp_path):
    async def scenario():
        profile = tmp_path / "profile"
        async with BrowserSession(profile=profile) as session:
            assert session.browser is not None
            page = session.context.pages[0]
            await page.goto(local_site.url)
            await page.evaluate("localStorage.setItem('sandbox-state', 'retained')")
            await session.context.add_cookies([{"name": "sandbox_session", "value": "retained",
                                               "url": local_site.url, "expires": time.time() + 86400}])
        async with BrowserSession(profile=profile) as session:
            page = session.context.pages[0]
            await page.goto(local_site.url)
            assert await page.evaluate("localStorage.getItem('sandbox-state')") == "retained"
            assert any(c["name"] == "sandbox_session" for c in await session.context.cookies())
            result = await run_task(Task("first", "127.0.0.1"), base_url=local_site.url, session=session,
                                    output=tmp_path / "artifacts", typing_delay_ms=0)
            assert result.status == "found", result
            metadata = json.loads((Path(result.artifacts) / "session.json").read_text())
            assert metadata["profile"] == str(profile)
            rows = [json.loads(line) for line in (Path(result.artifacts) / "network.jsonl").read_text().splitlines()]
            assert any(r["event"] == "response" and "/search" in r["url"] and r["http_status"] == 200 for r in rows)
    asyncio.run(scenario())


def test_navigation_after_runner_stops_is_captured_separately(local_site, tmp_path):
    from diagnostics import NetworkJournal
    async def scenario():
        async with BrowserSession() as session:
            result = await run_task(Task("captcha", "127.0.0.1"), base_url=local_site.url,
                                    session=session, output=tmp_path, typing_delay_ms=0)
            assert result.status == "captcha"
            journal = NetworkJournal(session.context, tmp_path / "inspection.jsonl")
            try:
                page = session.context.pages[0]
                await page.goto(local_site.url)
                await page.locator('input[name="text"]').fill("first")
                await page.locator('input[name="text"]').press("Enter")
                await page.wait_for_url("**/search?text=first")
            finally:
                journal.close()
            rows = [json.loads(line) for line in (tmp_path / "inspection.jsonl").read_text().splitlines()]
            assert any(r["event"] == "response" and "/search" in r["url"] and r["http_status"] == 200 for r in rows)
            assert result.status == "captcha"
    asyncio.run(scenario())
