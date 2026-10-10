import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scripts.swarm_v2.api.executions import ExecutionsAPI
from scripts.swarm_v2.api.tasks import TasksAPI
from scripts.swarm_v2.auth_context import GrantRefused

MAX_BODY_BYTES = 65_536
LENGTH = "Content-Length"
AUTHORIZATION = "Authorization"
JSON_CONTENT = ("Content-Type", "application/json")
TOO_LARGE = ("invalid_request", "the request body is too large")
NOT_JSON = ("invalid_request", "the request body is not JSON")
TASK_PATHS = re.compile(r"/v2/(?:tasks|swarm)(?:/.*)?")


class Routes:
    def __init__(self, executions: ExecutionsAPI, tasks: TasksAPI) -> None:
        self.executions, self.tasks = executions, tasks

    def route(self, method: str, path: str, authorization: str, body: object) -> tuple[int, dict]:
        api = self.tasks if TASK_PATHS.fullmatch(path) else self.executions
        return api.route(method, path, authorization, body)


class ExecutionsHandler(BaseHTTPRequestHandler):
    api: ExecutionsAPI | Routes

    def do_GET(self) -> None:
        self.reply(*self.api.route("GET", self.path, str(self.headers.get(AUTHORIZATION)), None))

    def do_POST(self) -> None:
        self.dispatch("POST")

    def do_PUT(self) -> None:
        self.dispatch("PUT")

    def do_PATCH(self) -> None:
        self.dispatch("PATCH")

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


def serve(api: ExecutionsAPI | Routes, host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), type(ExecutionsHandler.__name__, (ExecutionsHandler,), {"api": api}))
