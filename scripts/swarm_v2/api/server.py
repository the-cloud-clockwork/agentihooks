import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scripts.swarm_v2.api.executions import ExecutionsAPI
from scripts.swarm_v2.auth_context import GrantRefused

MAX_BODY_BYTES = 65_536
LENGTH = "Content-Length"
AUTHORIZATION = "Authorization"
JSON_CONTENT = ("Content-Type", "application/json")
TOO_LARGE = ("invalid_request", "the request body is too large")
NOT_JSON = ("invalid_request", "the request body is not JSON")


class ExecutionsHandler(BaseHTTPRequestHandler):
    api: ExecutionsAPI

    def do_POST(self) -> None:
        self.dispatch("POST")

    def do_PUT(self) -> None:
        self.dispatch("PUT")

    def dispatch(self, method: str) -> None:
        length = int(self.headers.get(LENGTH) or 0)
        if length > MAX_BODY_BYTES:
            self.reply(413, GrantRefused(*TOO_LARGE).detail())
            return
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            self.reply(400, GrantRefused(*NOT_JSON).detail())
            return
        self.reply(*self.api.route(method, self.path, str(self.headers.get(AUTHORIZATION)), body))

    def reply(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header(*JSON_CONTENT)
        self.send_header(LENGTH, str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return


def serve(api: ExecutionsAPI, host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), type(ExecutionsHandler.__name__, (ExecutionsHandler,), {"api": api}))
