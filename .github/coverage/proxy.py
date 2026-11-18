import os
import sys
import threading
from http.client import HTTPConnection, HTTPMessage, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

UPSTREAM_TIMEOUT = 30
IDLE: list[HTTPConnection] = []
IDLE_LOCK = threading.Lock()


def upstream(origin: str) -> HTTPConnection:
    host = urlsplit(origin)
    if host.scheme == "https":
        return HTTPSConnection(host.netloc, timeout=UPSTREAM_TIMEOUT)
    return HTTPConnection(host.netloc, timeout=UPSTREAM_TIMEOUT)


def exchange(method: str, target: str, body: bytes | None, headers: dict[str, str]) -> tuple[int, HTTPMessage, bytes]:
    origin = os.environ["SONAR_HOST_URL"].rstrip("/")
    while True:
        with IDLE_LOCK:
            pooled = IDLE.pop() if method == "GET" and IDLE else None
        connection = upstream(origin) if pooled is None else pooled
        try:
            connection.request(method, urlsplit(origin).path + target, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
        except Exception as error:
            connection.close()
            if pooled is None or not isinstance(error, OSError) or isinstance(error, TimeoutError):
                raise
            continue
        if method == "GET" and not response.will_close:
            with IDLE_LOCK:
                IDLE.append(connection)
        else:
            connection.close()
        return response.status, response.headers, payload


class SonarProxy(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.forward()

    def do_POST(self) -> None:
        self.forward()

    def forward(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in {"host", "connection", "transfer-encoding"}
        }
        headers["CF-Access-Client-Id"] = os.environ["CF_ACCESS_CLIENT_ID"]
        headers["CF-Access-Client-Secret"] = os.environ["CF_ACCESS_CLIENT_SECRET"]
        try:
            status, answer, payload = exchange(
                self.command, self.path, body if self.command == "POST" else None, headers
            )
        except OSError as error:
            status = 504 if "timed out" in str(error) else 502
            print(
                f"{self.command} {self.path} got no upstream answer, sent {status}: {error}",
                file=sys.stderr,
                flush=True,
            )
            self.send_error(status, "No answer from the Sonar server")
            return
        if 300 <= status < 400:
            self.send_error(502, "Upstream redirects are refused")
            return
        if status >= 400:
            refused_by = " ".join(f"{name}={answer.get(name, '-')}" for name in ("server", "cf-ray", "cf-mitigated"))
            print(f"{self.command} {self.path} answered {status} {refused_by}", file=sys.stderr, flush=True)
        self.send_response(status)
        for name, value in answer.items():
            if name.lower() not in {"connection", "transfer-encoding", "content-length"}:
                self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 9000), SonarProxy).serve_forever()
