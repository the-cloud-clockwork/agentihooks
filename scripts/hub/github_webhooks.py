import argparse
import hashlib
import hmac
import json
import os
import sys
import traceback
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scripts.events.config import BusConfig
from scripts.events.factory import open_bus
from scripts.hub.pulls import Observation, Pulls, suite_result

SECRET_ENV = "AGENTIHOOKS_GITHUB_WEBHOOK_SECRET"
NO_SECRET = f"{SECRET_ENV} must be set to verify GitHub webhooks"
PATH = "/github"
PORT = 8790
MAX_BODY = 25 * 1024 * 1024
REQUEST_TIMEOUT_S = 10
QUEUE_ACTIONS = {"enqueued": True, "dequeued": False}


def verify(secret: bytes, body: bytes, signature: str | None) -> bool:
    if not secret or not signature:
        return False
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature.encode(), expected.encode())


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


class Ingress:
    def __init__(self, pulls: Pulls, secret: bytes):
        if not secret:
            raise ValueError(NO_SECRET)
        self._pulls = pulls
        self._secret = secret

    def receive(self, headers: Mapping[str, str], body: bytes) -> int:
        if not verify(self._secret, body, headers.get("X-Hub-Signature-256")):
            return 401
        try:
            seen = observations(headers.get("X-GitHub-Event"), json.loads(body))
        except (ValueError, KeyError, TypeError):
            return 400
        try:
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

        def do_POST(self):
            length = str(self.headers.get("Content-Length"))
            if self.path != PATH:
                status = 404
            elif not length.isdecimal():
                status = 411
            elif int(length) > MAX_BODY:
                status = 413
            else:
                status = ingress.receive(self.headers, self.rfile.read(int(length)))
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            delivery, name = self.headers.get("X-GitHub-Delivery"), self.headers.get("X-GitHub-Event")
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
    bus = open_bus(BusConfig.from_environ(os.environ))
    server = ThreadingHTTPServer((args.host, args.port), handler(Ingress(Pulls(bus), secret)))
    try:
        server.serve_forever()
    finally:
        server.server_close()
        bus.close()
