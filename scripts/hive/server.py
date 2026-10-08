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


def _handler(redis: "Redis", redis_url: str) -> type[BaseHTTPRequestHandler]:
    class Join(BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT_S

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
                if not 0 < length <= MAX_BODY:
                    return None
                code = json.loads(self.rfile.read(length))["code"]
            except (ValueError, KeyError, TypeError):
                return None
            return code if isinstance(code, str) else None

        def _answer(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):
            pass

    return Join


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(socket.gethostbyname(host)).is_loopback
    except (OSError, ValueError):
        return False


def make_server(
    redis: "Redis", redis_url: str, host: str, port: int, tls: tuple[str, str] | None = None
) -> ThreadingHTTPServer:
    if tls is None and not is_loopback(host):
        raise auth.HiveError("a join endpoint off loopback hands out credentials, so it needs --tls-cert and --tls-key")
    context = None
    if tls is not None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            context.load_cert_chain(*tls)
        except (OSError, ssl.SSLError) as exc:
            raise auth.HiveError(f"the TLS certificate or key cannot be loaded ({exc})") from exc
    httpd = ThreadingHTTPServer((host, port), _handler(redis, redis_url))
    if context is not None:
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    return httpd
