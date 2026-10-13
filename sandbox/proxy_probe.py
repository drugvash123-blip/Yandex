"""Check supplied SOCKS5 credentials without printing them."""
import json
from pathlib import Path
import sys
from urllib.parse import quote, urlsplit

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Source/bots"))
from socks_bridge import SocksBridge


def main():
    proxy = json.loads(Path(sys.argv[1]).read_text())
    address = urlsplit(proxy["server"])
    url = f"socks5h://{quote(proxy['username'], safe='')}:{quote(proxy['password'], safe='')}@{address.hostname}:{address.port}"
    try:
        with requests.Session() as session:
            session.trust_env = False
            reply = session.get("https://api.ipify.org?format=json", proxies={"https": url}, timeout=15)
            reply.raise_for_status()
            print("SOCKS5 connected; exit IP:", reply.json()["ip"])
            with SocksBridge(proxy) as local:
                reply = session.get("https://api.ipify.org?format=json", proxies={"https": local["server"]}, timeout=15)
                reply.raise_for_status()
                print("CONNECT bridge connected; exit IP:", reply.json()["ip"])
        return 0
    except Exception as exc:
        print("Proxy probe failed:", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
