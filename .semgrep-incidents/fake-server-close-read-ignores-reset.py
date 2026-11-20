import threading
from http.server import BaseHTTPRequestHandler

closed = threading.Event()


class EmptyReadOnly(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(503)
        self.end_headers()
        # ruleid: fake-server-close-read-ignores-reset
        if self.rfile.read() == b"":
            closed.set()

    def do_HEAD(self) -> None:
        self.send_response(503)
        self.end_headers()
        # ruleid: fake-server-close-read-ignores-reset
        if not self.rfile.read(1):
            closed.set()

    def do_POST(self) -> None:
        self.send_response(503)
        self.end_headers()
        # ruleid: fake-server-close-read-ignores-reset
        assert self.rfile.read() == b""


class ResetCountsAsClosed(BaseHTTPRequestHandler):
    def closed_by_client(self) -> bool:
        try:
            # ok: fake-server-close-read-ignores-reset
            return self.rfile.read() == b""
        except ConnectionResetError:
            return True

    def gone(self) -> bool:
        try:
            # ok: fake-server-close-read-ignores-reset
            return not self.rfile.read(1)
        except OSError:
            return True


def test_a_bodiless_request_reaches_the_route_code(exchange: BaseHTTPRequestHandler) -> None:
    # ok: fake-server-close-read-ignores-reset
    assert exchange.rfile.read() == b""
