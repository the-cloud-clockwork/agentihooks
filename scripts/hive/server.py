"""The hive endpoint: POST /hive/refresh {"ledger_credential": ...} answers with a new Redis credential."""

import ipaddress
import json
import socket
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

from scripts.hive import auth

if TYPE_CHECKING:
    from collections.abc import Callable

    from redis import Redis

    from scripts.hive.nats_grant import Signer

MAX_BODY = 4096
REQUEST_TIMEOUT_S = 10
MAX_HANDLERS = 32
REQUEST_DEADLINE_S = 30
REDIS_TIMEOUT_S = 5
REDIS_STALLED = "the hive Redis did not answer; try again"
REFRESH_PATH = "/hive/refresh"
BAD_REFRESH_BODY = "the body must be a JSON object with a string ledger_credential"
ROUTES = {
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


def _grant(operation: str, redis: "Redis", value: str, redis_url: str, signer: "Signer | None") -> dict:
    if signer is None:
        return getattr(auth, operation)(redis, value, redis_url)
    from scripts.hive import nats_grant

    if signer.member_swarms is not None and (member := auth.member_of(redis, value)):
        nats_grant.require_listed(signer, member)
    grant = getattr(auth, operation)(redis, value, redis_url)
    grant = {**grant, "nats_credentials": nats_grant.mint(signer, grant["id"])}
    if signer.member_swarms is None:
        return {**grant, "bus_scoped": False}
    return {**grant, "bus_scoped": True, "bus_swarms": list(signer.member_swarms.get(grant["id"], ()))}


def _handler(
    redis: "Redis",
    redis_url: str,
    context: ssl.SSLContext | None,
    ledger_ca: str | None = None,
    signer: "Signer | Callable[[], Signer | None] | None" = None,
) -> type[BaseHTTPRequestHandler]:
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
            from redis.exceptions import ConnectionError as Unreachable
            from redis.exceptions import TimeoutError as Stalled

            try:
                current = signer() if callable(signer) else signer
                grant = _grant(operation, redis, value, redis_url, current)
                return self._answer(200, {**grant, "ledger_ca": ledger_ca} if ledger_ca else grant)
            except auth.HiveError as exc:
                if not isinstance(exc.__cause__, (Stalled, Unreachable)):
                    return self._answer(403, {"error": str(exc)})
            except (Stalled, Unreachable):
                pass
            return self._answer(503, {"error": REDIS_STALLED})

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
    def __init__(self, address, handler):
        self._slots = threading.BoundedSemaphore(MAX_HANDLERS)
        self._admitted: set[int] = set()
        self._open: dict[int, tuple[float, socket.socket]] = {}
        self._lock = threading.Lock()
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        while not self._slots.acquire(timeout=0.1):
            self.reap()
        with self._lock:
            self._admitted.add(id(request))
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            with self._lock:
                self._open[threading.get_ident()] = (time.monotonic() + REQUEST_DEADLINE_S, request.dup())
            super().process_request_thread(request, client_address)
        finally:
            with self._lock:
                entry = self._open.pop(threading.get_ident(), None)
                if entry is not None:
                    entry[1].close()
            if entry is None:
                self.shutdown_request(request)
            self._release(request)

    def _release(self, request):
        with self._lock:
            if id(request) not in self._admitted:
                return
            self._admitted.remove(id(request))
        self._slots.release()

    def reap(self) -> None:
        now = time.monotonic()
        with self._lock:
            for deadline, sock in self._open.values():
                if deadline <= now:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

    def service_actions(self):
        self.reap()

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], Dropped):
            super().handle_error(request, client_address)


def is_loopback(host: str | None) -> bool:
    try:
        return ipaddress.ip_address(socket.gethostbyname(host)).is_loopback
    except (OSError, TypeError, ValueError):
        return False


def make_server(
    redis: "Redis",
    redis_url: str,
    host: str,
    port: int,
    tls: tuple[str, str] | None = None,
    ledger_ca: str | None = None,
    signer: "Signer | Callable[[], Signer | None] | None" = None,
) -> ThreadingHTTPServer:
    if tls is None and not is_loopback(host):
        raise auth.HiveError("a hive endpoint off loopback hands out credentials, so it needs --tls-cert and --tls-key")
    context = None
    if tls is not None:
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        try:
            context.load_cert_chain(*tls)
        except (OSError, ssl.SSLError) as exc:
            raise auth.HiveError(f"the TLS certificate or key cannot be loaded ({exc})") from exc
    return JoinServer((host, port), _handler(bounded(redis), redis_url, context, ledger_ca, signer))


def bounded(redis: "Redis") -> "Redis":
    from redis.backoff import NoBackoff
    from redis.retry import Retry

    redis.get_connection_kwargs()["socket_timeout"] = REDIS_TIMEOUT_S
    redis.set_retry(Retry(NoBackoff(), 0))
    redis.connection_pool.reset()
    return redis
