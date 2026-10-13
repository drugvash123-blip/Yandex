"""Manual browser comparison: no automatic website navigation or task submission."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Source/bots"))
from runner import BrowserSession
from diagnostics import NetworkJournal, safe_url


async def run(args):
    proxy = json.loads(args.proxy_config.read_text()) if args.proxy_config else None
    directory = args.output / ("manual-" + uuid.uuid4().hex)
    directory.mkdir(parents=True, exist_ok=True)
    async with BrowserSession(headed=True, proxy=proxy, profile=args.profile) as session:
        page = session.context.pages[0] if session.context.pages else await session.context.new_page()
        await page.evaluate("document.title = 'Ручной тест Яндекса'")
        await page.bring_to_front()
        metadata = await session.metadata(page)
        (directory / "session.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
        journal = NetworkJournal(session.context, directory / "network.jsonl")
        closed = asyncio.Event()
        session.browser.on("disconnected", lambda *unused: closed.set())

        def request(request):
            host = urlsplit(request.url).hostname or ""
            if not request.is_navigation_request() or not any(host == domain or host.endswith("." + domain) for domain in ("ya.ru", "yandex.ru", "yandex.com")):
                return
            headers = request.headers
            permitted = ("user-agent", "accept-language", "accept", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "origin")
            record = {"url": safe_url(request.url), "headers": {key: headers[key] for key in permitted if key in headers}}
            if "referer" in headers:
                record["headers"]["referer"] = safe_url(headers["referer"])
            with (directory / "request-headers.jsonl").open("a") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        session.context.on("request", request)
        print(json.dumps({"status": "manual_browser_ready", "artifacts": str(directory.resolve()), "browser": metadata}, ensure_ascii=False), flush=True)
        try:
            if session.browser.is_connected():
                await closed.wait()
        finally:
            session.context.remove_listener("request", request)
            journal.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy-config", type=Path)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("sandbox/artifacts"))
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
