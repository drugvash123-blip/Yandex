"""Loopback HTTP/CONNECT bridge for an authenticated SOCKS5 upstream."""
import select
import socket
import socketserver
import threading
import logging
from urllib.parse import urlsplit

import socks

LOG = logging.getLogger(__name__)


class SocksBridge:
    def __init__(self, proxy, header_timeout=10):
        address = urlsplit(proxy["server"])
        if address.scheme != "socks5" or not address.hostname or not address.port:
            raise ValueError("Expected socks5://host:port")
        self.proxy = proxy
        self.address = address
        self.stopping = threading.Event()
        self.header_timeout = header_timeout

    def __enter__(self):
        bridge = self
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                upstream = None
                tunnel_open = False
                request_started = False
                self.connection.settimeout(bridge.header_timeout)
                try:
                    line = self.rfile.readline(8193).decode("ascii").strip().split()
                    if not line:
                        return
                    request_started = True
                    if len(line) != 3 or line[0] not in ("CONNECT", "GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
                        self.wfile.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                        return
                    size = 0
                    headers = []
                    length = 0
                    while True:
                        header = self.rfile.readline(8193)
                        size += len(header)
                        if size > 16384 or not header:
                            return
                        if header == b"\r\n":
                            break
                        name, value = header.split(b":", 1)
                        name = name.lower()
                        if name == b"transfer-encoding":
                            self.wfile.write(b"HTTP/1.1 501 Not Implemented\r\nConnection: close\r\n\r\n")
                            return
                        if name == b"content-length":
                            length = int(value.strip())
                        if name not in (b"proxy-authorization", b"proxy-connection", b"connection"):
                            headers.append(header)
                    if length < 0 or length > 8 * 1024 * 1024:
                        self.wfile.write(b"HTTP/1.1 413 Content Too Large\r\nConnection: close\r\n\r\n")
                        return
                    connect = line[0] == "CONNECT"
                    if connect:
                        host, port = line[1].rsplit(":", 1)
                    else:
                        destination = urlsplit(line[1])
                        if destination.scheme != "http" or not destination.hostname:
                            self.wfile.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
                            return
                        host, port = destination.hostname, destination.port or 80
                    upstream = socks.socksocket()
                    upstream.settimeout(45)
                    upstream.set_proxy(socks.SOCKS5, bridge.address.hostname, bridge.address.port,
                                       rdns=True, username=bridge.proxy.get("username"),
                                       password=bridge.proxy.get("password"))
                    upstream.connect((host.strip("[]"), int(port)))
                    if connect:
                        self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                        self.wfile.flush()
                    else:
                        path = destination.path or "/"
                        if destination.query:
                            path += "?" + destination.query
                        origin_line = f"{line[0]} {path} {line[2]}\r\n".encode("ascii")
                        upstream.sendall(origin_line + b"".join(headers) + b"Connection: close\r\n\r\n" + self.rfile.read(length))
                    tunnel_open = True
                    while not bridge.stopping.is_set():
                        ready, _, _ = select.select([self.connection, upstream] if connect else [upstream], [], [], 1)
                        for source in ready:
                            data = source.recv(65536)
                            if not data:
                                return
                            target = upstream if source is self.connection else self.connection
                            target.sendall(data)
                except Exception as exc:
                    if not tunnel_open and request_started:
                        LOG.warning("SOCKS connection failed: %s", type(exc).__name__)
                        try:
                            self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                        except OSError:
                            pass
                finally:
                    if upstream:
                        upstream.close()
        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
        self.server = Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return {"server": f"http://127.0.0.1:{self.server.server_address[1]}"}

    def __exit__(self, *args):
        self.stopping.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
