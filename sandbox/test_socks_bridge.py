import socket
import socketserver
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Source/bots"))
from socks_bridge import SocksBridge


def test_idle_connection_is_closed_without_a_synthetic_502():
    with SocksBridge({"server": "socks5://127.0.0.1:9"}, header_timeout=0.1) as local:
        port = int(local["server"].rsplit(":", 1)[1])
        with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
            assert client.recv(4096) == b""


def test_http_request_is_forwarded_instead_of_rejected():
    received = []
    class FakeSocks(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(5)
            _, count = self.rfile.read(2)
            self.rfile.read(count)
            self.wfile.write(b"\x05\x02")
            _, length = self.rfile.read(2)
            self.rfile.read(length)
            self.rfile.read(self.rfile.read(1)[0])
            self.wfile.write(b"\x01\x00")
            assert self.rfile.read(4) == b"\x05\x01\x00\x03"
            host = self.rfile.read(self.rfile.read(1)[0])
            port = int.from_bytes(self.rfile.read(2), "big")
            assert (host, port) == (b"example.test", 80)
            self.wfile.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50")
            while True:
                line = self.rfile.readline()
                received.append(line)
                if line == b"\r\n":
                    break
            self.wfile.write(b"HTTP/1.1 302 Found\r\nLocation: https://example.test/\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), FakeSocks) as upstream:
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        with SocksBridge({"server": f"socks5://127.0.0.1:{upstream.server_address[1]}", "username": "test", "password": "test"}) as local:
            port = int(local["server"].rsplit(":", 1)[1])
            with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
                client.sendall(b"GET http://example.test/search?text=test HTTP/1.1\r\nHost: example.test\r\nProxy-Authorization: secret\r\n\r\n")
                response = client.recv(4096)
                assert response.startswith(b"HTTP/1.1 302")
        upstream.shutdown()
        thread.join()
    assert received[0] == b"GET /search?text=test HTTP/1.1\r\n"
    assert b"Proxy-Authorization" not in b"".join(received)


def test_authenticated_socks_tunnel_roundtrip():
    received = {}
    class FakeSocks(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(5)
            version, count = self.rfile.read(2)
            methods = self.rfile.read(count)
            assert version == 5 and 2 in methods
            self.wfile.write(b"\x05\x02")
            version, length = self.rfile.read(2)
            user = self.rfile.read(length)
            password = self.rfile.read(self.rfile.read(1)[0])
            received["auth"] = (user, password)
            self.wfile.write(b"\x01\x00")
            header = self.rfile.read(4)
            assert header == b"\x05\x01\x00\x03"
            host = self.rfile.read(self.rfile.read(1)[0])
            port = int.from_bytes(self.rfile.read(2), "big")
            received["destination"] = (host, port)
            self.wfile.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50")
            data = self.rfile.read(4)
            self.wfile.write(data)
    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), FakeSocks) as upstream:
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        proxy = {"server": f"socks5://127.0.0.1:{upstream.server_address[1]}",
                 "username": "sandbox-user", "password": "sandbox-password"}
        with SocksBridge(proxy) as local:
            port = int(local["server"].rsplit(":", 1)[1])
            with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
                client.sendall(b"CONNECT example.test:443 HTTP/1.1\r\nHost: example.test:443\r\n\r\n")
                response = b""
                while b"\r\n\r\n" not in response:
                    response += client.recv(4096)
                assert response.startswith(b"HTTP/1.1 200")
                client.sendall(b"ping")
                assert client.recv(4) == b"ping"
        upstream.shutdown()
        thread.join()
    assert received["auth"] == (b"sandbox-user", b"sandbox-password")
    assert received["destination"] == (b"example.test", 443)
