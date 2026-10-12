"""Grafana alert webhooks in, standing stage findings out: a firing stage alert updates its finding, a resolve clears it.

Only numbers and known names are taken from an alert; its free text never reaches the swarm.
"""

from __future__ import annotations

import ipaddress
import json
import math
import re
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
HOST = "0.0.0.0"
PORT = 8796
PATH = "/alerts"
MAX_BODY = 1_000_000
EPISODE_CHARS = 40
UNIT = "task hours in the window"
STAGES = (
    "claim_wait",
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


def settings(environ: Mapping[str, str]) -> Settings:
    sources = [part.strip() for part in environ.get(FROM_ENV, "").split(",") if part.strip()]
    return Settings(
        environ.get(HOST_ENV) or HOST,
        int(environ.get(PORT_ENV) or PORT),
        tuple(ipaddress.ip_network(part) for part in sources) or None,
    )


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
    if not found.port:
        print(f"alert intake off: {PORT_ENV} is 0", file=sys.stderr)
        return False
    server = HTTPServer((found.host, found.port), Handler)
    server.redis, server.networks, server.clock = redis, found.networks, clock
    server.serve_forever()
    return True
