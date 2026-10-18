"""Grafana alert webhooks in, standing stage findings out: a firing stage alert updates its finding, a resolve clears it.

Only numbers and known names are taken from an alert; its free text never reaches the swarm.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import math
import re
import ssl
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING

from scripts.swarm.health import standing

if TYPE_CHECKING:
    from redis import Redis

HOST_ENV = "AGENTIHOOKS_ALERT_INTAKE_HOST"
PORT_ENV = "AGENTIHOOKS_ALERT_INTAKE_PORT"
FROM_ENV = "AGENTIHOOKS_ALERT_INTAKE_FROM"
TOKEN_ENV = "AGENTIHOOKS_ALERT_INTAKE_TOKEN"
CERT_ENV = "AGENTIHOOKS_ALERT_INTAKE_CERT"
KEY_ENV = "AGENTIHOOKS_ALERT_INTAKE_KEY"
HOST = "0.0.0.0"
PORT = 8796
PATH = "/alerts"
MAX_BODY = 1_000_000
EPISODE_CHARS = 40
UNIT = "task hours in the window"
STAGES = (
    "seat_wait",
    "review",
    "merge_queue",
    "intent_fix",
    "launch_failure",
    "ci_queue",
    "quota_hold",
    "host_hold",
)
SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    networks: tuple | None
    token: str
    cert: str
    key: str


def settings(environ: Mapping[str, str]) -> Settings:
    sources = [part.strip() for part in environ.get(FROM_ENV, "").split(",") if part.strip()]
    return Settings(
        environ.get(HOST_ENV) or HOST,
        int(environ.get(PORT_ENV) or PORT),
        tuple(ipaddress.ip_network(part) for part in sources) or None,
        environ.get(TOKEN_ENV, "").strip(),
        environ.get(CERT_ENV, "").strip(),
        environ.get(KEY_ENV, "").strip(),
    )


def authorized(header: str | None, token: str) -> bool:
    return bool(token) and hmac.compare_digest(str(header).encode(), f"Bearer {token}".encode())


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def refusal(found: Settings) -> str:
    if not found.port:
        return f"alert intake off: {PORT_ENV} is 0"
    if not found.token:
        return f"alert intake off: {TOKEN_ENV} is not set"
    if not (found.cert and found.key) and not _loopback(found.host):
        return f"alert intake off: {found.host} is not loopback, so it needs {CERT_ENV} and {KEY_ENV}"
    return ""


def allowed(address: str, networks: tuple | None) -> bool:
    try:
        source = ipaddress.ip_address(address)
    except ValueError:
        return False
    if networks is None:
        return source.is_private
    return any(source in network for network in networks)


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _length(raw) -> int | None:
    try:
        length = int(raw or 0)
    except ValueError:
        return None
    return length if length >= 0 else None


def _apply(redis, alert, now_ms: int) -> str:
    if not isinstance(alert, dict):
        return "skipped an alert: not an alert"
    labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
    stage, slug, status = str(labels.get("stage")), str(labels.get("ledger")), alert.get("status")
    if labels.get("swarm") != "bottleneck":
        return "skipped an alert: not a swarm bottleneck alert"
    if stage not in STAGES:
        return f"skipped an alert: unknown stage {stage}"
    if not SLUG.fullmatch(slug):
        return "skipped an alert: bad ledger"
    if status == "resolved":
        if standing.clear(redis, slug, stage):
            return f"cleared {stage} on {slug}"
        return f"no {stage} finding on {slug}"
    if status != "firing":
        return f"skipped an alert: unknown status {status}"
    values = alert.get("values")
    hours = _number(values.get("hours")) if isinstance(values, dict) else None
    if hours is None:
        return "skipped an alert: no hours value"
    threshold = _number(labels.get("threshold_hours"))
    if threshold is None:
        return "skipped an alert: no threshold"
    episode = str(alert.get("startsAt", ""))[:EPISODE_CHARS]
    standing.fire(redis, slug, stage, standing.Reading(hours, threshold, UNIT, episode), now_ms)
    return f"fired {stage} on {slug}"


def receive(redis: Redis, payload: object, now_ms: int) -> list[str]:
    if not isinstance(payload, dict):
        raise ValueError("the alert body is not a JSON object")
    alerts = payload.get("alerts") or []
    if not isinstance(alerts, list):
        raise ValueError("the alerts are not a list")
    return [_apply(redis, alert, now_ms) for alert in alerts]


class Handler(BaseHTTPRequestHandler):
    timeout = 10

    def _reply(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        from redis.exceptions import RedisError

        if not allowed(self.client_address[0], self.server.networks):
            return self._reply(403, {"error": "source not allowed"})
        if not authorized(self.headers.get("Authorization"), self.server.token):
            return self._reply(401, {"error": "missing or wrong token"})
        if self.path != PATH:
            return self._reply(404, {"error": "not found"})
        length = _length(self.headers.get("Content-Length"))
        if length is None:
            return self._reply(400, {"error": "bad content length"})
        if length > MAX_BODY:
            return self._reply(413, {"error": "body too large"})
        try:
            actions = receive(self.server.redis, json.loads(self.rfile.read(length)), self.server.clock())
        except ValueError as exc:
            return self._reply(400, {"error": f"bad alert body: {exc}"})
        except RedisError:
            return self._reply(503, {"error": "swarm store unreachable"})
        return self._reply(200, {"actions": actions})


def serve(redis: Redis, found: Settings, clock: Callable[[], int]) -> bool:
    off = refusal(found)
    if off:
        print(off, file=sys.stderr)
        return False
    server = HTTPServer((found.host, found.port), Handler)
    if found.cert and found.key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(found.cert, found.key)
        # The handshake runs on the handler's first read, so the handler timeout bounds a stalled client.
        server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    server.redis, server.networks, server.clock, server.token = redis, found.networks, clock, found.token
    server.serve_forever()
    return True
