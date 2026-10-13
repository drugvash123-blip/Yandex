"""Launch ordinary Chrome without Playwright or a remote-debugging connection."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Source/bots"))
from runner import BrowserSession
from socks_bridge import SocksBridge


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy-config", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    args = parser.parse_args()
    proxy = json.loads(args.proxy_config.read_text())
    BrowserSession(proxy=proxy, profile=args.profile).prepare_profile()
    directory = Path("sandbox/artifacts") / ("native-manual-" + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    token = uuid.uuid4().hex
    html = '''<!doctype html><meta charset=utf-8><meta name="referrer" content="no-referrer">
    <title>Ручной тест — обычный Chrome</title>
    <h1>Ручной тест — обычный Chrome</h1>
    <p>Playwright не управляет этим браузером. Откройте Яндекс самостоятельно.</p>
    <a href="https://ya.ru/">Открыть Яндекс</a><pre id=info></pre>
    <script>
    const info = {user_agent:navigator.userAgent, webdriver:navigator.webdriver,
      language:navigator.language, platform:navigator.platform,
      timezone:Intl.DateTimeFormat().resolvedOptions().timeZone};
    document.querySelector('#info').textContent=JSON.stringify(info,null,2);
    fetch('/environment/TOKEN', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(info)});
    </script>'''.replace("TOKEN", token).encode()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path != "/" + token:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            if self.path != "/environment/" + token or length > 8192:
                self.send_error(400)
                return
            info = json.loads(self.rfile.read(length))
            info = {key: info.get(key) for key in ("user_agent", "webdriver", "language", "platform", "timezone")}
            (directory / "environment.json").write_text(json.dumps(info, ensure_ascii=False, indent=2))
            print(json.dumps({"status": "native_browser_ready", "environment": info,
                              "artifacts": str(directory.resolve())}, ensure_ascii=False), flush=True)
            self.send_response(204)
            self.end_headers()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with SocksBridge(proxy) as local:
            chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            command = [chrome, "--user-data-dir=" + str(args.profile.resolve()),
                       "--proxy-server=" + local["server"], "--no-first-run", "--no-default-browser-check",
                       "--disable-background-mode", f"http://127.0.0.1:{server.server_port}/{token}"]
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("Ordinary Chrome launched; no automatic Yandex navigation", flush=True)
            try:
                process.wait()
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
