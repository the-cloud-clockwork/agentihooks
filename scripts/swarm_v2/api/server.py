import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scripts.swarm_v2.api.executions import ExecutionsAPI
from scripts.swarm_v2.auth_context import GrantRefused

MAX_BODY_BYTES = 65_536


class ExecutionsHandler(BaseHTTPRequestHandler):
    api: ExecutionsAPI

    def do_GET(self) -> None:
        self.dispatch("GET")

    def do_POST(self) -> None:
        self.dispatch("POST")

    def do_PUT(self) -> None:
        self.dispatch("PUT")

    def dispatch(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self.reply(413, GrantRefused("invalid_request", "the request body is too large").detail())
            return
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            body = None
        self.reply(*self.api.route(method, self.path, self.headers.get("Authorization", ""), body))

    def reply(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return


def serve(api: ExecutionsAPI, host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), type("BoundExecutionsHandler", (ExecutionsHandler,), {"api": api}))
