import threading
from collections.abc import Callable, Iterable, Mapping
from http.server import ThreadingHTTPServer
from pathlib import Path

from scripts.swarm.store import RedisStore
from scripts.swarm_v2.api.executions import ExecutionsAPI
from scripts.swarm_v2.api.server import serve
from scripts.swarm_v2.auth_context import MIN_KEY_BYTES, LaunchAuthority, LaunchKey
from scripts.swarm_v2.authority import TaskAuthority
from scripts.swarm_v2.controller import Controller
from scripts.swarm_v2.runtime.operations import OperationTransport

KEY_ID_ENV = "AGENTIHOOKS_LAUNCH_SIGNING_KEY_ID"
KEY_FILE_ENV = "AGENTIHOOKS_LAUNCH_SIGNING_KEY_FILE"
ISSUER = "controller"
AUDIENCE = "workers"


class ControlError(Exception):
    pass


def launch_key(environ: Mapping[str, str]) -> LaunchKey:
    missing = [name for name in (KEY_ID_ENV, KEY_FILE_ENV) if not environ.get(name)]
    if missing:
        raise ControlError(f"the launch signing key needs {', '.join(missing)}")
    try:
        secret = Path(environ[KEY_FILE_ENV]).read_bytes().strip()
    except OSError as error:
        raise ControlError(f"the launch signing key file is unreadable: {error.strerror}") from None
    if len(secret) < MIN_KEY_BYTES:
        raise ControlError(f"the launch signing key must be at least {MIN_KEY_BYTES} bytes")
    try:
        return LaunchKey(environ[KEY_ID_ENV], secret)
    except ValueError as error:
        raise ControlError(str(error)) from None


class ControlService:
    def __init__(
        self,
        store: RedisStore,
        slug: str,
        key: LaunchKey,
        authorize: Callable[[], bool],
        transports: Iterable[OperationTransport] = (),
    ) -> None:
        self.controller = Controller(store, slug, transports, authorize)
        self.grants = LaunchAuthority(store, key, ISSUER, AUDIENCE)
        self.tasks = TaskAuthority(store, self.controller, lambda token: self.grants.bound(slug, token))
        self.executions = ExecutionsAPI(self.grants, self.tasks)

    def start(self) -> bool:
        return self.controller.acquire()

    def tick(self) -> bool:
        return self.controller.renew()

    def serve(self, host: str, port: int) -> ThreadingHTTPServer:
        server = serve(self.executions, host, port)
        threading.Thread(target=server.serve_forever, name="swarm-v2-api", daemon=True).start()
        return server
