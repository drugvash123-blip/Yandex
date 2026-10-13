"""Finite browser search runner. No production configuration is read on import."""
import argparse
import asyncio
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass, field as data_field
from datetime import datetime, timezone
import json
import logging
import hashlib
import os
import random
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, urlunsplit
import unicodedata
import uuid

from playwright.async_api import Error as BrowserError, TimeoutError as BrowserTimeoutError, async_playwright
from diagnostics import NetworkJournal, safe_error, safe_url
from captcha_detection import inspect_captcha

LOG = logging.getLogger(__name__)


def hostname(value: str) -> str:
    parsed = urlsplit(value if "://" in value else "https://" + value)
    if not parsed.hostname:
        raise ValueError("Expected a domain or URL")
    return parsed.hostname.rstrip(".").lower().encode("idna").decode("ascii")


def matches_domain(url: str, domain: str) -> bool:
    actual, expected = hostname(url), hostname(domain)
    return actual == expected or actual.endswith("." + expected)


@dataclass(frozen=True)
class Task:
    query: str
    domain: str | None = None

    def __post_init__(self):
        if not self.query.strip():
            raise ValueError("Query cannot be empty")
        if self.domain is not None:
            hostname(self.domain)


@dataclass
class Result:
    query: str
    domain: str | None
    status: str = "error"
    page: int | None = None
    position: int | None = None
    url: str | None = None
    visited_url: str | None = None
    http_status: int | None = None
    detail: str | None = None
    timestamp: str = ""
    artifacts: str = ""
    stage: str = "start"
    pages_checked: int = 0
    search_url: str | None = None
    pagination: str | None = None
    actions: list[dict] = data_field(default_factory=list)
    search_complete: bool = False
    completion_reason: str | None = None
    page_checks: list[dict] = data_field(default_factory=list)
    captcha_events: list[dict] = data_field(default_factory=list)


@dataclass
class CaptchaBudget:
    used: bool = False


async def captcha_present(page):
    try:
        return ("showcaptcha" in page.url or "captcha" in urlsplit(page.url).path.lower()
                or "Вы не робот" in await page.title()
                or await page.locator(".CheckboxCaptcha, .AdvancedCaptcha, #captcha-slider").count() > 0)
    except BrowserError as exc:
        # A redirect after manual completion can replace the JS context during this check.
        if not page.is_closed() and "Execution context was destroyed" in str(exc):
            return True  # Retry during the bounded wait instead of aborting the task.
        raise


SEARCH_INPUT = 'input[name="text"]:visible, textarea[name="text"]:visible, input.input__control:visible, input[type="search"]:visible'
# Vertical blocks (for example Images) share h2 markup with website results.
AD_REDIRECT_LINKS = ', '.join(f'a[href^="{scheme}//yabs.yandex.ru/count/"]' for scheme in ('https:', 'http:', ''))
RESULT_CARDS = f'.serp-item:not(.serp-item_type_ad):not(:has(.UniSearchHeader-Title)):not(:has({AD_REDIRECT_LINKS}))'
RESULT_LINKS = ', '.join(f'{RESULT_CARDS} {anchor}' for anchor in ('a.OrganicTitle-Link', 'h2 a', 'a.organic__url'))


def typing_timeout_ms(text, delay):
    return max(10000, len(text) * delay + 5000)


def completed_search(result, max_pages):
    return result.status == "found" or (result.status == "not_found" and result.search_complete
                                        and result.completion_reason in ("page_limit", "end_of_results"))


def query_matches(url, query):
    def normalize(value):
        return " ".join(unicodedata.normalize("NFC", value).split())
    values = parse_qs(urlsplit(url).query).get("text", [])
    return len(values) == 1 and normalize(values[0]) == normalize(query)


async def read_result_rows(links):
    """One website title per card; retain locator indexes for actual clicks."""
    return await links.evaluate_all("""els => {
                const seen = new Set();
                return els.flatMap((e, i) => {
                    const card = e.closest('.serp-item');
                    if (seen.has(card)) return [];
                    seen.add(card);
                    return [{url:e.href, text:e.textContent, locatorIndex:i}];
                });
    }""")


def result_signature(rows):
    return tuple(urlunsplit(urlsplit(row["url"])._replace(fragment="")) for row in rows)


async def terminal_pager(page, current):
    # A missing Next selector alone is not evidence that the results ended.
    return await page.evaluate("""current => {
        const visible = e => !!e && e.getClientRects().length > 0;
        const pager = [...document.querySelectorAll('.Pager, nav[aria-label="Pagination"], nav[aria-label="Пагинация"]')].find(visible);
        if (!pager) return false;
        const active = pager.querySelector('[aria-current="page"], .Pager-Item_current');
        if (!visible(active) || Number(active.textContent.trim()) !== current) return false;
        const controls = [...pager.querySelectorAll('a, button')].filter(visible);
        const enabled = controls.filter(e => !e.disabled && e.getAttribute('aria-disabled') !== 'true');
        // Only understood backward/numeric controls are allowed on a terminal pager.
        if (enabled.some(e => {
            const text = e.textContent.trim();
            const n = Number(text);
            if (/^\\d+$/.test(text)) return n > current;
            return !e.matches('[rel="prev"], .Pager-Item_type_prev') && !/^(назад|предыдущая|previous)$/i.test(text);
        })) return false;
        const disabledNext = controls.some(e => (e.disabled || e.getAttribute('aria-disabled') === 'true') &&
            (e.matches('[rel="next"], .Pager-Item_type_next') || /^(дальше|следующая|next)$/i.test(e.textContent.trim())));
        const numbered = controls.filter(e => /^\\d+$/.test(e.textContent.trim())).map(e => Number(e.textContent.trim()));
        return disabledNext || (numbered.length > 0 && Math.max(...numbered, current) === current &&
            (current === 1 || numbered.some(n => n < current)));
    }""", current)


async def wait_captcha(page, result, seconds, handler=None, budget=None):
    if not await captcha_present(page):
        return True
    capture_id = 'captcha-' + uuid.uuid4().hex[:8]
    captured = set()
    async def capture():
        if not result.artifacts:
            return
        try:
            frames = []
            for frame in page.frames:
                structure = await frame.evaluate("""() => ({
                    selectors: Object.fromEntries(['.CheckboxCaptcha-Button', '.AdvancedCaptcha',
                        '.AdvancedCaptcha-View', '.Textinput input', '.CaptchaButton_view_action',
                        '#captcha-slider'].map(s=>[s,document.querySelectorAll(s).length])),
                    images: Array.from(document.querySelectorAll('.AdvancedCaptcha img')).map(img=>({
                        loaded:img.complete && img.naturalWidth>0, width:img.naturalWidth,height:img.naturalHeight,
                        source:img.currentSrc || img.src}))
                })""")
                for img in structure['images']:
                    img['source_digest'] = hashlib.sha256(img.pop('source').encode()).hexdigest()
                frames.append({'url': safe_url(frame.url), **structure})
            detection = await inspect_captcha(page)
            signature = json.dumps({'frames': frames, 'detection': detection}, sort_keys=True)
            if signature in captured:
                return
            name = f'{capture_id}-{len(captured)}'
            directory = Path(result.artifacts)
            (directory / f'{name}.json').write_text(json.dumps(frames, ensure_ascii=False, indent=2))
            (directory / f'{name}-diagnosis.json').write_text(json.dumps(detection, ensure_ascii=False, indent=2))
            await save_snapshot(page, directory, name)
            for i, frame in enumerate(page.frames):
                (directory / f'{name}-frame-{i}.html').write_text(await frame.content(), encoding='utf-8')
            captured.add(signature)
            LOG.info('CAPTCHA structure saved: %s', name)
        except (BrowserError, OSError):
            LOG.info('CAPTCHA changed during capture; continuing observation')
    await capture()
    if handler is not None and budget is not None and not budget.used:
        # Consume before calling: a transport failure may already create a paid task.
        budget.used = True
        for _ in range(3):
            result.captcha_events.append({'event': 'handler_attempt', 'stage': result.stage})
            outcome = None
            try:
                outcome = await handler(page)
                if isinstance(outcome, dict) and outcome.get('outcome') in ('explicit_confirmation', 'captcha_left', 'challenge_changed'):
                    result.captcha_events.append({'event': 'handler_outcome', 'outcome': outcome['outcome']})
                result.captcha_events.append({'event': 'handler_returned', 'verified': False})
            except Exception as error:
                from captcha_service import CaptchaError
                code = str(error) if isinstance(error, CaptchaError) else safe_error(error)
                result.captcha_events.append({'event': 'handler_failed', 'code': code})
                LOG.info('CAPTCHA handler stopped (%s); manual completion remains available', code)
            if result.artifacts:
                try:
                    (Path(result.artifacts) / 'captcha-events.json').write_text(json.dumps(result.captcha_events, ensure_ascii=False, indent=2), encoding='utf-8')
                except OSError:
                    LOG.warning('Could not save CAPTCHA events; continuing manual observation')
            await capture()
            # Only a changed challenge after a completed submission can start
            # another paid stage. Transport/format failures never trigger this.
            if not (isinstance(outcome, dict) and outcome.get('outcome') == 'challenge_changed'
                    and getattr(handler, 'remaining_attempts', 0) > 0):
                break
            LOG.info('New CAPTCHA stage; remaining paid limit: %s', handler.remaining_attempts)
            await asyncio.sleep(2)
    if seconds:
        LOG.info("Complete CAPTCHA in the browser; waiting up to %s seconds", seconds)
        try:
            async with asyncio.timeout(seconds):
                while await captcha_present(page):
                    await capture()
                    await asyncio.sleep(0.5)
        except TimeoutError:
            pass
    if await captcha_present(page):
        result.status, result.detail = "captcha", "Browser requires CAPTCHA completion"
        return False
    if result.artifacts:
        await save_snapshot(page, Path(result.artifacts), capture_id + '-after')
    return True


async def search(page, task, result, base_url, max_pages, click, captcha_wait=0, directory=None, typing_delay_ms=0,
                 action_pause=0, visit_seconds=0, captcha_handler=None):
    document = {"status": None}
    navigation_status = {}
    navigation_status_by_url = {}
    recovery_used = False
    seen_results = set()
    captcha_budget = CaptchaBudget()
    def track_response(response):
        if response.request.is_navigation_request():
            navigation_status_by_url[response.url] = response.status
            try:
                owner = response.frame.page
            except BrowserError:
                return
            if response.frame == owner.main_frame:
                navigation_status[owner] = response.status
                if owner is page:
                    document["status"] = response.status
                    result.http_status = response.status
    page.context.on("response", track_response)

    def stage(value):
        result.stage = value
        LOG.info("Stage: %s", value)

    def action(description):
        result.actions.append({"time": datetime.now(timezone.utc).isoformat(), "description": description})
        LOG.info("Action: %s", description)

    async def pause():
        if action_pause:
            action(f"Пауза {action_pause:g} с")
            await asyncio.sleep(action_pause)

    try:
        async def http_failure():
            nonlocal recovery_used
            result.http_status = document["status"]
            text = await page.locator("body").inner_text(timeout=5000)
            if result.http_status == 429 or "доступ к сервисам Яндекса временно заблокирован" in text:
                recovery_wait = min(captcha_wait, 120) if not recovery_used else 0
                if recovery_wait:
                    recovery_used = True
                    action(f"HTTP 429: ожидание восстановления страницы до {recovery_wait:g} с без повторных запросов runner")
                    end = asyncio.get_running_loop().time() + recovery_wait
                    while asyncio.get_running_loop().time() < end:
                        await asyncio.sleep(min(1, max(0, end - asyncio.get_running_loop().time())))
                        if document["status"] is None or not 200 <= document["status"] < 300:
                            continue
                        try:
                            text = await page.locator("body").inner_text(timeout=1000)
                            ready = await page.locator(RESULT_LINKS if result.stage == "results" else SEARCH_INPUT).first.is_visible()
                            if ready and "доступ к сервисам Яндекса временно заблокирован" not in text and not await captcha_present(page):
                                result.http_status = document["status"]
                                action(f"Страница восстановилась: HTTP {result.http_status}; продолжение задания")
                                if action_pause:
                                    seconds = max(30, action_pause)
                                    action(f"Пауза после восстановления {seconds:g} с")
                                    await asyncio.sleep(seconds)
                                    return await http_failure()
                                return False
                        except BrowserError:
                            if page.is_closed():
                                raise
                result.status, result.detail = "rate_limited", "Current browser request received a rate limit response"
                return True
            if result.http_status and result.http_status >= 400:
                result.status, result.detail = "http_error", f"Document returned HTTP {result.http_status}"
                return True
            return False

        stage("homepage")
        action("Открытие главной Яндекса")
        try:
            await page.goto(base_url, wait_until="commit")
        except BrowserTimeoutError:
            if not await captcha_present(page):
                result.status, result.detail = "navigation_timeout", "Homepage navigation exceeded its timeout"
                return
        await page.locator("body").wait_for(state="attached", timeout=15000)
        if not await wait_captcha(page, result, captcha_wait, captcha_handler, captcha_budget) or await http_failure():
            return
        # The original scenario enters the query through the homepage's real search form.
        close = page.locator('.Distribution-ButtonClose:visible').first
        if await close.count():
            await close.click()
        field = page.locator(SEARCH_INPUT).first
        try:
            await field.wait_for(state="visible", timeout=60000)
        except BrowserTimeoutError:
            result.status, result.detail = "unexpected_page", "Homepage search input was not found"
            return
        stage("submit_query")
        await pause()
        action("Клик по полю поиска и очистка поля")
        await field.click()
        await field.fill("")
        action(f"Посимвольный ввод запроса с интервалом {typing_delay_ms} мс")
        await field.press_sequentially(task.query, delay=typing_delay_ms,
                                       timeout=typing_timeout_ms(task.query, typing_delay_ms))
        await pause()
        action("Нажатие Enter для отправки запроса")
        await field.press("Enter", no_wait_after=True)
        try:
            await page.wait_for_url(lambda url: "/search" in urlsplit(url).path or "captcha" in urlsplit(url).path,
                                    wait_until="commit", timeout=60000)
        except BrowserTimeoutError:
            if not await captcha_present(page):
                result.status, result.detail = "submission_failed", "Search form did not open search results"
                return

        for index in range(max_pages):
            stage("results")
            if index and await captcha_present(page):
                result.status, result.detail = "captcha", "CAPTCHA during pagination; further navigation stopped"
                return
            if not await wait_captcha(page, result, captcha_wait, captcha_handler, captcha_budget) or await http_failure():
                return
            links = page.locator(RESULT_LINKS)
            try:
                await links.first.wait_for(state="visible", timeout=15000)
            except BrowserTimeoutError:
                if await http_failure():
                    return
                result.status = "captcha" if await captcha_present(page) else "unexpected_page"
                result.detail = "No recognized organic results; inspect HTML/screenshot"
                return
            if action_pause:
                seconds = max(15, action_pause)
                action(f"Ожидание перед просмотром результатов {seconds:g} с")
                await asyncio.sleep(seconds)
                if await captcha_present(page):
                    result.status, result.detail = "captcha", "CAPTCHA appeared while waiting for results"
                    return
                if await http_failure():
                    return
            # Keep one title per result card; breadcrumbs and duplicate anchors are not positions.
            rows = await read_result_rows(links)
            if not query_matches(page.url, task.query):
                result.status, result.detail = "incomplete", "Results URL does not confirm the requested query"
                return
            parameters = parse_qs(urlsplit(page.url).query)
            if result.pagination != "more" and parameters.get("p", ["0"]) != [str(index)]:
                result.status, result.detail = "incomplete", "Results page number does not match the requested page"
                return
            signature = result_signature(rows)
            fingerprint = tuple(sorted(set(signature)))
            if not signature or fingerprint in seen_results:
                result.status, result.detail = "incomplete", "Empty or repeated results; page coverage cannot be confirmed"
                return
            seen_results.add(fingerprint)
            result.page_checks.append({"page": index + 1, "query_matches": True,
                                       "organic_count": len(rows), "urls": list(signature),
                                       "pagination": result.pagination})
            result.pages_checked = index + 1
            result.search_url = page.url
            action(f"Страница/порция {index + 1}: проверено {len(rows)} органических результатов")
            if directory:
                (directory / f"results-{index + 1}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
                await save_snapshot(page, directory, f"results-{index + 1}")
            random_row = random.choice(rows) if task.domain is None and rows else None
            for position, row in enumerate(rows, 1):
                try:
                    expected_domain = task.domain or hostname(row["url"])
                    matches = matches_domain(row["url"], task.domain) if task.domain else row is random_row
                except ValueError:
                    matches = False
                if matches:
                    action(f"Найден целевой результат: страница {index + 1}, позиция {position}")
                    result.page, result.position, result.url = index + 1, position, row["url"]
                    if directory:
                        await save_snapshot(page, directory, "search")
                    if click:
                        stage("visit_site")
                        selected = links.nth(row["locatorIndex"])
                        action("Прокрутка к ссылке найденного сайта")
                        if action_pause:
                            await selected.evaluate("el => el.scrollIntoView({behavior: 'smooth', block: 'center'})")
                        else:
                            await selected.scroll_into_view_if_needed()
                        await pause()
                        action("Клик по найденному сайту")
                        old_pages = set(page.context.pages)
                        source_url = page.url
                        await selected.click(no_wait_after=True)
                        destination = None
                        end = asyncio.get_running_loop().time() + 60
                        while asyncio.get_running_loop().time() < end:
                            candidates = [p for p in page.context.pages if p is page or p not in old_pages]
                            for candidate in candidates:
                                if (not candidate.is_closed() and (candidate is not page or candidate.url != source_url)
                                        and matches_domain(candidate.url, expected_domain)):
                                    destination = candidate
                                    break
                            if destination:
                                break
                            await asyncio.sleep(0.25)
                        if destination is None:
                            result.status, result.detail = "navigation_failed", "Result click did not reach the expected domain"
                            return
                        try:
                            await destination.wait_for_url(lambda url: matches_domain(url, expected_domain),
                                                           wait_until="commit", timeout=60000)
                            await destination.locator("body").wait_for(state="visible", timeout=30000)
                        except BrowserTimeoutError:
                            result.status, result.detail = "navigation_failed", "Result click did not reach the expected domain"
                            return
                        result.visited_url = destination.url
                        destination_status = navigation_status_by_url.get(destination.url, navigation_status.get(destination))
                        if destination_status is None or not 200 <= destination_status < 300:
                            result.status, result.detail = "navigation_failed", f"Destination document status: {destination_status}"
                            return
                        action(f"Подтверждён переход на {expected_domain}")
                        if visit_seconds:
                            action(f"Ожидание на сайте {visit_seconds:g} с")
                            await asyncio.sleep(visit_seconds)
                    result.status = "found"
                    result.search_complete, result.completion_reason = True, "target_found"
                    stage("complete")
                    return
            if index + 1 == max_pages:
                result.search_complete, result.completion_reason = True, "page_limit"
                break
            stage("pagination")
            numbered = page.get_by_role("link", name=str(index + 2), exact=True)
            next_link = page.locator('.Pager-Item_type_next:not([aria-disabled="true"]):not([disabled]):visible, a[rel="next"]:not([aria-disabled="true"]):visible')
            more = page.get_by_role("button", name="Показать ещё", exact=True)
            if await numbered.count() and await numbered.first.is_visible() and await numbered.first.is_enabled():
                control = numbered.first
                result.pagination = "numbered"
            elif await next_link.count():
                control = next_link.first
                result.pagination = "numbered"
            elif await more.count() and await more.first.is_visible() and await more.first.is_enabled():
                control = more.first
                result.pagination = "more"
            else:
                if await terminal_pager(page, index + 1):
                    result.search_complete, result.completion_reason = True, "end_of_results"
                    result.page_checks[-1]["end_evidence"] = "recognized_terminal_pager"
                    action("Подтверждён конец выдачи по состоянию пагинации")
                    break
                result.status, result.detail = "incomplete", "No next control and no recognized evidence of results ending"
                return
            previous = await links.evaluate_all("els => els.map(e => e.href).join('\\n')")
            previous_url = page.url
            action(f"Прокрутка к элементу перехода на страницу/порцию {index + 2}")
            if action_pause:
                await control.evaluate("el => el.scrollIntoView({behavior: 'smooth', block: 'center'})")
            else:
                await control.scroll_into_view_if_needed()
            await pause()
            action(f"Клик по пагинации: {result.pagination}")
            await control.click(no_wait_after=True)
            try:
                await page.wait_for_function("""({selector, old, url}) =>
                    !!document.querySelector('.CheckboxCaptcha, .AdvancedCaptcha') ||
                    location.pathname.toLowerCase().includes('captcha') ||
                    /429|временно заблокирован/.test(document.title) ||
                    Array.from(document.querySelectorAll(selector)).map(e=>e.href).join('\\n') !== old
                    """, arg={"selector": RESULT_LINKS, "old": previous, "url": previous_url}, timeout=15000)
            except BrowserTimeoutError:
                result.status, result.detail = "pagination_stalled", "Pagination did not update the results"
                return
            await pause()
        result.status = "not_found"
        stage("complete")

    finally:
        page.context.remove_listener("response", track_response)


class BrowserSession:
    """One proxy/browser session for an entire finite batch, as in the original worker."""
    def __init__(self, *, headed=False, mobile=False, channel="chrome", proxy=None, profile=None):
        self.headed, self.mobile, self.channel, self.proxy = headed, mobile, channel, proxy
        self.profile = Path(profile).resolve() if profile else None
        self.session_id = uuid.uuid4().hex
        self.pw = self.browser = self.context = self.bridge = None

    def prepare_profile(self):
        identity = {"server": (self.proxy or {}).get("server"), "username": (self.proxy or {}).get("username"),
                    "mobile": self.mobile, "channel": self.channel}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        marker = self.profile / ".runner-profile.json"
        if self.profile.exists() and any(self.profile.iterdir()):
            if not marker.exists() or json.loads(marker.read_text()).get("identity") != digest:
                raise ValueError("Use a separate runner profile for this browser/proxy configuration")
        self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker.write_text(json.dumps({"identity": digest}))

    async def metadata(self, page=None):
        data = {"session_id": self.session_id, "channel": self.channel, "headed": self.headed,
                "mobile": self.mobile, "profile": str(self.profile) if self.profile else None,
                "proxy": safe_url(self.proxy["server"]) if self.proxy else None,
                "cookie_count": len(await self.context.cookies()),
                "browser_version": self.browser.version if self.browser else None}
        if page:
            try:
                data["environment"] = await asyncio.wait_for(page.evaluate("""() => ({
                    user_agent: navigator.userAgent, webdriver: navigator.webdriver,
                    language: navigator.language, platform: navigator.platform,
                    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone
                })"""), timeout=3)
            except Exception:
                data["environment"] = None
        return data

    async def __aenter__(self):
        try:
            if self.profile:
                self.prepare_profile()
            proxy = self.proxy
            if proxy and proxy["server"].startswith("socks5://") and proxy.get("username"):
                from socks_bridge import SocksBridge
                self.bridge = SocksBridge(proxy)
                proxy = self.bridge.__enter__()
            self.pw = await async_playwright().start()
            settings = self.pw.devices["Pixel 7"] if self.mobile else {"viewport": {"width": 1280, "height": 900}}
            launch = {"channel": self.channel, "headless": not self.headed, "proxy": proxy,
                      "args": [] if proxy else ["--no-proxy-server"], "timeout": 15000}
            if self.profile:
                self.context = await self.pw.chromium.launch_persistent_context(str(self.profile), **launch,
                                                                              **settings, locale="ru-RU")
                self.browser = self.context.browser
            else:
                self.browser = await self.pw.chromium.launch(**launch)
                self.context = await self.browser.new_context(**settings, locale="ru-RU")
            self.context.set_default_timeout(10000)
            self.context.set_default_navigation_timeout(60000)
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *args):
        for name, resource in (("context", self.context), ("browser", self.browser), ("playwright", self.pw)):
            if resource:
                try:
                    action = resource.stop if name == "playwright" else resource.close
                    await asyncio.wait_for(action(), timeout=5)
                except Exception:
                    LOG.warning("Could not close %s", name)
        if self.bridge:
            self.bridge.__exit__(None, None, None)


async def run_task(task, *, base_url="https://ya.ru/", max_pages=3,
                   deadline=60, headed=False, mobile=False, click=True,
                   channel="chrome", proxy=None, output=Path("sandbox/artifacts"), captcha_wait=0,
                   session=None, typing_delay_ms=1000, action_pause=0, visit_seconds=0, captcha_handler=None):
    output = Path(output)
    directory = output / uuid.uuid4().hex
    directory.mkdir(parents=True, exist_ok=True)
    result = Result(task.query, task.domain, timestamp=datetime.now(timezone.utc).isoformat(), artifacts=str(directory.resolve()))
    async with AsyncExitStack() as stack:
        context = None
        tracing = False
        journal = None
        try:
            async with asyncio.timeout(deadline):
                if session is None:
                    session = await stack.enter_async_context(BrowserSession(headed=headed, mobile=mobile,
                                                                            channel=channel, proxy=proxy))
                context = session.context
                journal = NetworkJournal(context, directory / "network.jsonl")
                await context.tracing.start(screenshots=True, snapshots=True, sources=True)
                tracing = True
                page = context.pages[0] if context.pages else await context.new_page()
                (directory / "session.json").write_text(json.dumps(await session.metadata(page), ensure_ascii=False, indent=2), encoding="utf-8")
                await search(page, task, result, base_url, max_pages, click, captcha_wait, directory, typing_delay_ms,
                             action_pause, visit_seconds, captcha_handler)
        except TimeoutError:
            result.status, result.detail = "timeout", "Task deadline exceeded"
        except Exception as exc:
            result.status, result.detail = "error", safe_error(exc)
        finally:
            if context:
                try:
                    await asyncio.wait_for(save_page(context, directory), timeout=5)
                except Exception:
                    LOG.warning("Could not save page diagnostics")
                if tracing:
                    try:
                        await asyncio.wait_for(context.tracing.stop(path=str(directory / "trace.zip")), timeout=5)
                    except Exception:
                        LOG.warning("Could not save trace")
                # Return to the search tab after visiting the site; keep cookies for the next task.
                for extra in context.pages[1:]:
                    try:
                        await asyncio.wait_for(extra.close(), timeout=3)
                    except Exception:
                        LOG.warning("Could not close result tab")
                if journal:
                    journal.close()
    (directory / "result.json").write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8")
    with (output / "results.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")
    return result


async def save_snapshot(page, directory, name):
    try:
        (directory / f"{name}.html").write_text(await page.content(), encoding="utf-8")
        await page.screenshot(path=str(directory / f"{name}.png"), full_page=True, timeout=3000)
    except BrowserError:
        LOG.warning("Could not save %s snapshot", name)


async def save_page(context, directory):
    if context.pages:
        await save_snapshot(context.pages[-1], directory, "page")


async def wait_for_tasks(service, wait_seconds, poll_interval):
    """Bounded, sequential reads; never retries a statistics write."""
    end = asyncio.get_running_loop().time() + wait_seconds
    attempt = 0
    while True:
        attempt += 1
        tasks = await asyncio.to_thread(service.fetch)
        response = getattr(service, 'last_response', None) or {}
        LOG.info('Task fetch %s: received %s; HTTP %s; body length %s', attempt, len(tasks),
                 response.get('http_status'), len(response.get('body', '')))
        if tasks or not wait_seconds:
            return tasks
        remaining = end - asyncio.get_running_loop().time()
        if remaining <= 0:
            return []
        await asyncio.sleep(min(poll_interval, remaining))
        if asyncio.get_running_loop().time() >= end:
            return []


def main(argv=None):
    parser = argparse.ArgumentParser(description="Finite Playwright search runner")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--query")
    source.add_argument("--tasks-file", type=Path, help="JSON array of query/domain tasks")
    source.add_argument("--fetch-tasks", action="store_true", help="Fetch one batch from the original service")
    parser.add_argument("--wait-for-tasks", type=float, default=0, help="Seconds to wait for nonempty server tasks")
    parser.add_argument("--task-poll-interval", type=float, default=60)
    parser.add_argument("--captcha-key-file", type=Path, help="Explicit opt-in to at most one paid CAPTCHA per batch")
    parser.add_argument("--captcha-max-tasks", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--captcha-kinds", choices=("text", "silhouette", "both"), default="both")
    parser.add_argument("--captcha-endpoint", default="https://api.cap.guru")
    parser.add_argument("--domain", help="Target domain; omitted for a random organic result")
    parser.add_argument("--limit-tasks", type=int, default=10)
    parser.add_argument("--upload-stats", action="store_true")
    parser.add_argument("--client-id", help="Non-secret identifier for service statistics")
    parser.add_argument("--service-key-file", type=Path, help="Local file containing the task service key")
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--deadline", type=float, default=600)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--profile", type=Path, help="Dedicated persistent runner profile (never use your normal Chrome profile)")
    parser.add_argument("--keep-open-on-failure", action="store_true", help="Keep a headed browser open until the user closes it after a failed task")
    parser.add_argument("--mobile", action="store_true")
    visit = parser.add_mutually_exclusive_group()
    visit.add_argument("--click", action="store_true", dest="click")
    visit.add_argument("--no-click", action="store_false", dest="click")
    parser.set_defaults(click=True)
    parser.add_argument("--captcha-wait", type=float, default=0, help="Seconds for manual CAPTCHA completion (requires --headed)")
    parser.add_argument("--typing-delay-ms", type=int, default=1000, help="Delay between keystrokes; original behaviour: 1000 ms")
    parser.add_argument("--action-pause", type=float, default=15, help="Pause before query submission and pagination actions")
    parser.add_argument("--visit-seconds", type=float, default=100, help="Time on destination site, matching the original worker")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--output", type=Path, default=Path("sandbox/artifacts"))
    connection = parser.add_mutually_exclusive_group()
    connection.add_argument("--proxy-config", type=Path, help="Local JSON: server, username, password")
    connection.add_argument("--no-proxy", action="store_true", help="Ignore proxy configuration and environment")
    args = parser.parse_args(argv)
    if not 1 <= args.repeat <= 10 or not 1 <= args.max_pages <= 10 or args.deadline <= 0:
        parser.error("repeat/max-pages must be 1..10; deadline must be positive")
    if args.captcha_wait < 0 or (args.captcha_wait and not args.headed):
        parser.error("captcha-wait must be nonnegative and requires --headed")
    if args.keep_open_on_failure and not args.headed:
        parser.error("keep-open-on-failure requires headed")
    if args.domain and not args.query:
        parser.error("domain is only used with query; batch tasks specify their own domains")
    if args.limit_tasks < 1 or args.limit_tasks > 100:
        parser.error("limit-tasks must be 1..100")
    if args.upload_stats and not args.client_id:
        parser.error("upload-stats requires client-id")
    if not 0 <= args.typing_delay_ms <= 2000:
        parser.error("typing-delay-ms must be 0..2000")
    if args.action_pause < 0 or args.visit_seconds < 0:
        parser.error("action-pause and visit-seconds must be nonnegative")
    if args.wait_for_tasks < 0 or args.task_poll_interval < 30 or (args.wait_for_tasks and not args.fetch_tasks):
        parser.error("wait-for-tasks requires fetch-tasks; poll interval must be at least 30 seconds")
    if args.captcha_key_file and args.deadline < 180:
        parser.error("Paid CAPTCHA requires a task deadline of at least 180 seconds")
    captcha_key = None
    service = None
    try:
        if args.captcha_key_file:
            from captcha_service import CaptchaService
            captcha_key = args.captcha_key_file.read_text(encoding="utf-8").strip()
            # Validate without making requests.
            with CaptchaService(captcha_key, endpoint=args.captcha_endpoint):
                pass
        if args.fetch_tasks or args.upload_stats:
            from task_service import TaskService
            key = args.service_key_file.read_text(encoding="utf-8").strip() if args.service_key_file else os.environ.get("BOT_SERVICE_KEY")
            service = TaskService(key)
        if args.fetch_tasks:
            tasks = []
        elif args.tasks_file:
            data = json.loads(args.tasks_file.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError("Tasks file must contain a JSON array")
            tasks = [Task(**item) for item in data]
        else:
            tasks = [Task(args.query, args.domain)]
        proxy = None
        if args.proxy_config:
            proxy = json.loads(args.proxy_config.read_text(encoding="utf-8"))
            if not isinstance(proxy, dict) or not isinstance(proxy.get("server"), str):
                raise ValueError("Proxy configuration requires a server string")
        elif not args.no_proxy and os.environ.get("BROWSER_PROXY_SERVER"):
            proxy = {"server": os.environ["BROWSER_PROXY_SERVER"]}
            for field in ("username", "password"):
                value = os.environ.get("BROWSER_PROXY_" + field.upper())
                if value:
                    proxy[field] = value
    except (ValueError, TypeError, OSError) as exc:
        parser.error("Invalid configuration: " + type(exc).__name__)
    args.output.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(args.output / "runner.log", encoding="utf-8")])

    async def batch():
        results = []
        failed = False
        solver = None
        try:
            batch_tasks = await wait_for_tasks(service, args.wait_for_tasks, args.task_poll_interval) if args.fetch_tasks else tasks
            if not batch_tasks:
                LOG.info("No tasks in batch; waiting limit reached" if args.wait_for_tasks else "No tasks in batch")
                return 2 if args.wait_for_tasks else 0
            if args.fetch_tasks:
                (args.output / 'received-tasks.json').write_text(json.dumps([asdict(task) for task in batch_tasks], ensure_ascii=False, indent=2), encoding='utf-8')
            if captcha_key:
                from captcha_service import CaptchaQuota
                solver = CaptchaQuota(captcha_key, endpoint=args.captcha_endpoint, max_tasks=args.captcha_max_tasks)
            async with BrowserSession(headed=args.headed, mobile=args.mobile, proxy=proxy, profile=args.profile) as session:
                for task in batch_tasks[:args.limit_tasks]:
                    for _ in range(args.repeat):
                        captcha_handler = None
                        if solver is not None and not solver.exhausted:
                            from captcha_handler import PaidSearchCaptchaHandler
                            kinds = ("text", "silhouette") if args.captcha_kinds == "both" else (args.captcha_kinds,)
                            captcha_handler = PaidSearchCaptchaHandler(solver, allowed_kinds=kinds)
                        result = await run_task(task, max_pages=args.max_pages, deadline=args.deadline,
                                                headed=args.headed, mobile=args.mobile, click=args.click,
                                                proxy=proxy, output=args.output, captcha_wait=args.captcha_wait, session=session,
                                                typing_delay_ms=args.typing_delay_ms, action_pause=args.action_pause,
                                                visit_seconds=args.visit_seconds, captcha_handler=captcha_handler)
                        results.append(result)
                        print(json.dumps(asdict(result), ensure_ascii=False), flush=True)
                        completed = completed_search(result, args.max_pages)
                        failed |= not completed
                        if args.keep_open_on_failure and not completed:
                            closed = asyncio.Event()
                            session.browser.on("disconnected", lambda *unused: closed.set())
                            inspection = NetworkJournal(session.context, Path(result.artifacts) / "inspection-network.jsonl")
                            try:
                                if session.context.pages:
                                    page = session.context.pages[0]
                                    try:
                                        await asyncio.wait_for(page.evaluate("document.title = 'Runner — ручная проверка'"), timeout=3)
                                        await asyncio.wait_for(page.bring_to_front(), timeout=3)
                                    except Exception:
                                        LOG.warning("Could not label the inspection tab; the browser remains open")
                                    LOG.info("Inspection window: Runner — ручная проверка; %s", safe_url(page.url))
                                LOG.info("Runner stopped controlling the page; manual navigation is recorded until you close this browser")
                                if session.browser.is_connected():
                                    await closed.wait()
                            finally:
                                inspection.close()
                            break
                        if result.status in ("rate_limited", "captcha"):
                            LOG.warning("Stopping batch: search requires manual intervention")
                            break
                    if results[-1].status in ("rate_limited", "captcha") or (args.keep_open_on_failure and not completed_search(results[-1], args.max_pages)):
                        break
            if service and args.upload_stats:
                submitted = await asyncio.to_thread(service.submit, results, args.client_id)
                args.output.mkdir(parents=True, exist_ok=True)
                receipt = {"submitted": submitted, "client_id": args.client_id,
                           "response": service.last_response if submitted else None}
                receipt_path = args.output / ("submission-" + uuid.uuid4().hex + ".json")
                receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
                LOG.info("Statistics submission: %s", "completed" if submitted else "no eligible results")
            return int(failed)
        except Exception as exc:
            error = safe_error(exc)
            args.output.mkdir(parents=True, exist_ok=True)
            failure = {"time": datetime.now(timezone.utc).isoformat(), "error": error,
                       "completed_tasks": len(results), "profile": str(args.profile) if args.profile else None,
                       "proxy": safe_url(proxy["server"]) if proxy else None}
            (args.output / ("batch-error-" + uuid.uuid4().hex + ".json")).write_text(json.dumps(failure, ensure_ascii=False, indent=2), encoding="utf-8")
            LOG.error("Batch failed: %s; saved results are retained", error)
            return 2
        finally:
            if solver is not None:
                (args.output / 'captcha-service.json').write_text(json.dumps({
                    'submission_attempted': solver.submitted, 'task_id': solver.task_id, 'exchanges': solver.exchanges}, indent=2), encoding='utf-8')
                solver.__exit__(None, None, None)
            if service:
                service.session.close()
    return asyncio.run(batch())


if __name__ == "__main__":
    raise SystemExit(main())
