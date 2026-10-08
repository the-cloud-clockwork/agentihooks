"""The hive join endpoint: POST /hive/join {"code": ...} answers with the member's credentials."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scripts.hive import auth

JOIN_PATH = "/hive/join"


def _handler(redis, redis_url):
    class Join(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != JOIN_PATH:
                return self._answer(404, {"error": "not found"})
            try:
                code = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))["code"]
            except (ValueError, KeyError, TypeError):
                return self._answer(400, {"error": "the body must be JSON with a code"})
            try:
                return self._answer(200, auth.exchange(redis, code, redis_url))
            except auth.HiveError as exc:
                return self._answer(403, {"error": str(exc)})

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


def make_server(redis, redis_url: str, host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), _handler(redis, redis_url))
