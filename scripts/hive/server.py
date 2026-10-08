"""The hive join endpoint: POST /hive/join {"code": ...} answers with the member's credentials."""

import ipaddress
import json
import socket
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

from scripts.hive import auth

if TYPE_CHECKING:
    from redis import Redis

JOIN_PATH = "/hive/join"
MAX_BODY = 4096
REQUEST_TIMEOUT_S = 10
BAD_BODY = "the body must be a JSON object with a string code"


def _handler(redis: "Redis", redis_url: str, context: ssl.SSLContext | None) -> type[BaseHTTPRequestHandler]:
    class Join(BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT_S

        def setup(self):
            if context is not None:
                self.request.settimeout(self.timeout)
                self.request = context.wrap_socket(self.request, server_side=True)
            super().setup()

        def do_POST(self):
            if self.path != JOIN_PATH:
                return self._answer(404, {"error": "not found"})
            code = self._code()
            if code is None:
                return self._answer(400, {"error": BAD_BODY})
            try:
                return self._answer(200, auth.exchange(redis, code, redis_url))
            except auth.HiveError as exc:
                return self._answer(403, {"error": str(exc)})

        def _code(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length not in range(MAX_BODY + 1):
                    return None
                code = json.loads(self.rfile.read(length))["code"]
            except (ValueError, KeyError, TypeError):
                return None
            return code if isinstance(code, str) else None

        def _answer(self, status, body):
            self.send_response(status)
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, format, *args):
            pass

    return Join


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
    return ThreadingHTTPServer((host, port), _handler(redis, redis_url, context))
