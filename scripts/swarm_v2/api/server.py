import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scripts.swarm_v2.api.artifacts import MAX_REQUEST, ArtifactsAPI
from scripts.swarm_v2.api.artifacts import PATH as ARTIFACT_PATH
from scripts.swarm_v2.api.broadcasts import BroadcastsAPI
from scripts.swarm_v2.api.commands import CommandsAPI
from scripts.swarm_v2.api.executions import ExecutionsAPI
from scripts.swarm_v2.api.health import HealthAPI
from scripts.swarm_v2.api.recovery import PATH as RECOVERY_PATH
from scripts.swarm_v2.api.recovery import RecoveryAPI
from scripts.swarm_v2.api.tasks import TasksAPI
from scripts.swarm_v2.auth_context import GrantRefused

MAX_BODY_BYTES = 65_536
LENGTH = "Content-Length"
AUTHORIZATION = "Authorization"
JSON_CONTENT = ("Content-Type", "application/json")
TOO_LARGE = ("invalid_request", "the request body is too large")
NOT_JSON = ("invalid_request", "the request body is not JSON")
TASK_PATHS = re.compile(r"/v2/(?:tasks|swarm)(?:/.*)?")
BROADCAST_PATHS = re.compile(r"/v2/broadcasts(?:/.*)?")
COMMAND_PATHS = re.compile(r"/v2/executions/[^/]+/commands(?:/.*)?")
HEALTH_PATHS = re.compile(r"/v2/health/.*")


class Routes:
    def __init__(
        self,
        executions: ExecutionsAPI,
        tasks: TasksAPI | None,
        commands: CommandsAPI | None = None,
        broadcasts: BroadcastsAPI | None = None,
        health: HealthAPI | None = None,
        recovery: RecoveryAPI | None = None,
    ) -> None:
        self.executions, self.tasks, self.commands, self.broadcasts = executions, tasks, commands, broadcasts
        self.health, self.recovery = health, recovery
        self.artifacts: ArtifactsAPI | None = None

    def route(self, method: str, path: str, authorization: str, body: object) -> tuple[int, dict]:
        api = self.tasks if self.tasks is not None and TASK_PATHS.fullmatch(path) else self.executions
        if self.broadcasts is not None and BROADCAST_PATHS.fullmatch(path):
            api = self.broadcasts
        if self.commands is not None and COMMAND_PATHS.fullmatch(path):
            api = self.commands
        if self.health is not None and HEALTH_PATHS.fullmatch(path):
            api = self.health
        if self.recovery is not None and RECOVERY_PATH.fullmatch(path):
            api = self.recovery
        if self.artifacts is not None and path == ARTIFACT_PATH:
            api = self.artifacts
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
        limit = MAX_REQUEST if self.path == ARTIFACT_PATH else MAX_BODY_BYTES
        if length > limit:
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
