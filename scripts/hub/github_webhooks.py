import argparse
import hmac
import json
import os
import sys
import threading
import traceback
from collections.abc import Mapping
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from scripts.ci_lab.head_notice import signature as signed
from scripts.ci_lab.webhooks import NOTICE_SECRET_ENV, LabIntake, envelope, head_events, notice_event
from scripts.events.bus import Bus
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.factory import open_bus
from scripts.hub.pulls import Observation, Pulls, suite_result

SECRET_ENV = "AGENTIHOOKS_GITHUB_WEBHOOK_SECRET"
NO_SECRET = f"{SECRET_ENV} must be set to verify GitHub webhooks"
PATH = "/github"
NOTICE_PATH = "/lab/head"
ROUTES = (PATH, NOTICE_PATH)
SIGNATURE = "X-Hub-Signature-256"
PORT = 8790
MAX_BODY = 25 * 1024 * 1024
REQUEST_TIMEOUT_S = 10
QUEUE_ACTIONS = {"enqueued": True, "dequeued": False}
LOGGED_HEADERS = ("X-GitHub-Delivery", "X-GitHub-Event")
LOGGED_HEADER_CHARS = 64


def verify(secret: bytes, body: bytes, signature: str | None) -> bool:
    if not secret or not signature:
        return False
    return hmac.compare_digest(signature.encode(), signed(secret, body).encode())


def observations(name: str | None, payload: dict) -> list[Observation]:
    if name == "pull_request":
        pull = payload["pull_request"]
        state = "merged" if pull["merged"] else pull["state"]
        return [Observation(pull["html_url"], state, pull["head"]["sha"], QUEUE_ACTIONS.get(payload["action"]))]
    if name == "check_suite" and payload["action"] == "completed":
        suite = payload["check_suite"]
        result = {str(suite["id"]): suite_result(suite["app"], suite["conclusion"])}
        repository = payload["repository"]["html_url"]
        return [
            Observation(f"{repository}/pull/{pull['number']}", checks_head=suite["head_sha"], suites=result)
            for pull in suite["pull_requests"]
        ]
    return []


class LazyPulls:
    def __init__(self, bus: Bus):
        self._bus = bus
        self._pulls: Pulls | None = None
        self._lock = threading.Lock()

    def observe(self, seen: Observation) -> None:
        with self._lock:
            if self._pulls is None:
                self._pulls = Pulls(self._bus)
        self._pulls.observe(seen)


class Ingress:
    def __init__(self, pulls: Pulls | LazyPulls, secret: bytes, lab: LabIntake | None = None) -> None:
        if not secret:
            raise ValueError(NO_SECRET)
        self._pulls = pulls
        self._secret = secret
        self._lab = lab

    def receive(self, headers: Mapping[str, str], body: bytes) -> int:
        if not verify(self._secret, body, headers.get(SIGNATURE)):
            return 401
        name = headers.get("X-GitHub-Event")
        try:
            payload = json.loads(body)
            seen = observations(name, payload)
        except (ValueError, KeyError, TypeError):
            return 400
        heads = self._heads(name, payload)
        status = self._record(seen, heads or [])
        return 400 if heads is None and status == 200 else status

    def _heads(self, name: str | None, payload: Any) -> list[Envelope] | None:
        if self._lab is None:
            return []
        try:
            return [envelope(head) for head in head_events(name, payload)]
        except (ValueError, KeyError, TypeError):
            return None

    def notice(self, headers: Mapping[str, str], body: bytes) -> int:
        """Answer a lab head notice from the agents' push path: 404 without the lab, 401 unless the lab secret signed it."""
        if self._lab is None:
            return 404
        if not verify(self._lab.secret, body, headers.get(SIGNATURE)):
            return 401
        try:
            heads = [envelope(notice_event(json.loads(body)))]
        except (ValueError, KeyError, TypeError):
            return 400
        return self._record([], heads)

    def _record(self, seen: list[Observation], heads: list[Envelope]) -> int:
        try:
            if self._lab is not None:
                self._lab.publish(heads)
            for observed in seen:
                self._pulls.observe(observed)
        except Exception:
            print("hub: GitHub webhook could not record the event", file=sys.stderr)
            traceback.print_exc()
            return 503
        return 200


def handler(ingress: Ingress) -> type[BaseHTTPRequestHandler]:
    class Webhook(BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT_S

        def do_POST(self) -> None:
            length = str(self.headers.get("Content-Length"))
            if self.path not in ROUTES:
                status = 404
            elif not length.isdecimal():
                status = 411
            elif int(length) > MAX_BODY:
                status = 413
            else:
                answer = ingress.receive if self.path == PATH else ingress.notice
                status = answer(self.headers, self.rfile.read(int(length)))
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            delivery, name = (str(self.headers.get(key))[:LOGGED_HEADER_CHARS] for key in LOGGED_HEADERS)
            print(f"github-webhook: delivery {delivery} event {name} answered {status}", file=sys.stderr)

        def log_message(self, format, *args):
            pass

    return Webhook


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agentihooks github-webhook")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args(argv)
    secret = os.environ.get(SECRET_ENV, "").encode()
    if not secret:
        print(f"agentihooks github-webhook: {NO_SECRET}", file=sys.stderr)
        return 2
    bus = open_bus(replace(BusConfig.from_environ(os.environ), provision=False))
    lab_secret = os.environ.get(NOTICE_SECRET_ENV, "").encode()
    lab = LabIntake(bus.lab_heads, lab_secret) if lab_secret else None
    server = ThreadingHTTPServer((args.host, args.port), handler(Ingress(LazyPulls(bus), secret, lab)))
    try:
        server.serve_forever()
    finally:
        server.server_close()
        bus.close()
