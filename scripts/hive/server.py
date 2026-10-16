"""The hive join endpoint: POST /hive/join {"code": ...} answers with the member's credentials."""

import ipaddress
import json
import socket
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

from scripts.hive import auth

if TYPE_CHECKING:
    from redis import Redis

JOIN_PATH = "/hive/join"
MAX_BODY = 4096
REQUEST_TIMEOUT_S = 10
REFRESH_PATH = "/hive/refresh"
BAD_BODY = "the body must be a JSON object with a string code"
BAD_REFRESH_BODY = "the body must be a JSON object with a string ledger_credential"
ROUTES = {
    JOIN_PATH: ("code", BAD_BODY, "exchange"),
    REFRESH_PATH: ("ledger_credential", BAD_REFRESH_BODY, "refresh"),
}


class Dropped(Exception):
    pass


class _TLSHandler(BaseHTTPRequestHandler):
    tls: ssl.SSLContext | None = None

    def setup(self):
        if self.tls is not None:
            self.request.settimeout(self.timeout)
            try:
                self.request = self.tls.wrap_socket(self.request, server_side=True)
            except OSError as exc:
                raise Dropped from exc
        super().setup()

    def finish(self):
        super().finish()
        if isinstance(self.request, ssl.SSLSocket):
            try:
                self.request.unwrap()
            except OSError:
                pass


def _handler(redis: "Redis", redis_url: str, context: ssl.SSLContext | None) -> type[BaseHTTPRequestHandler]:
    class Join(_TLSHandler):
        timeout = REQUEST_TIMEOUT_S
        tls = context

        def do_POST(self):
            if self.path not in ROUTES:
                return self._answer(404, {"error": "not found"})
            field, bad_body, operation = ROUTES[self.path]
            value = self._field(field)
            if value is None:
                return self._answer(400, {"error": bad_body})
            try:
                return self._answer(200, getattr(auth, operation)(redis, value, redis_url))
            except auth.HiveError as exc:
                return self._answer(403, {"error": str(exc)})

        def _field(self, field):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length not in range(MAX_BODY + 1):
                    return None
                value = json.loads(self.rfile.read(length))[field]
            except (ValueError, KeyError, TypeError):
                return None
            return value if isinstance(value, str) else None

        def _answer(self, status, body):
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            pass

    return Join


class JoinServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], Dropped):
            super().handle_error(request, client_address)


def is_loopback(host: str | None) -> bool:
    try:
        return ipaddress.ip_address(socket.gethostbyname(host)).is_loopback
    except (OSError, TypeError, ValueError):
        return False


def make_server(
    redis: "Redis", redis_url: str, host: str, port: int, tls: tuple[str, str] | None = None
) -> ThreadingHTTPServer:
    if tls is None and not is_loopback(host):
        raise auth.HiveError("a join endpoint off loopback hands out credentials, so it needs --tls-cert and --tls-key")
    context = None
    if tls is not None:
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        try:
            context.load_cert_chain(*tls)
        except (OSError, ssl.SSLError) as exc:
            raise auth.HiveError(f"the TLS certificate or key cannot be loaded ({exc})") from exc
    return JoinServer((host, port), _handler(redis, redis_url, context))
