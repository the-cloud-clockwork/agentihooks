import json
import os
import threading
from collections.abc import Callable, Iterable, Mapping
from http.server import ThreadingHTTPServer
from pathlib import Path

from scripts.hive import auth as hive_auth
from scripts.swarm import lease
from scripts.swarm.store import AgentRecord, RedisStore, SwarmError
from scripts.swarm_v2 import deployed
from scripts.swarm_v2.api.executions import ExecutionsAPI
from scripts.swarm_v2.api.server import serve
from scripts.swarm_v2.auth_context import LaunchAuthority, LaunchKey
from scripts.swarm_v2.authority import TaskAuthority
from scripts.swarm_v2.controller import Controller
from scripts.swarm_v2.kubernetes.watch import BACKEND
from scripts.swarm_v2.reconciliation.accounts import Finding
from scripts.swarm_v2.runtime.observe import Observer, Reading, Signal, Source, Thresholds
from scripts.swarm_v2.runtime.operations import OperationTransport

KEY_ID_ENV = "AGENTIHOOKS_LAUNCH_SIGNING_KEY_ID"
KEY_FILE_ENV = "AGENTIHOOKS_LAUNCH_SIGNING_KEY_FILE"
PORT_ENV = "AGENTIHOOKS_CONTROL_API_PORT"
SWARM_ENV = "AGENTIHOOKS_CONTROL_SWARM"
CREDENTIAL_ENV = "AGENTIHOOKS_CONTROLLER_CREDENTIAL"
HOST = "0.0.0.0"
MAX_PORT = 65535
THREAD = "swarm-v2-api"
ISSUER = "controller"
AUDIENCE = "workers"


class ControlError(SwarmError):
    pass


def launch_key(environ: Mapping[str, str]) -> LaunchKey:
    missing = [name for name in (KEY_ID_ENV, KEY_FILE_ENV) if not environ.get(name)]
    if missing:
        raise ControlError(f"the launch signing key needs {', '.join(missing)}")
    try:
        secret = Path(environ[KEY_FILE_ENV]).read_bytes().strip()
    except OSError as error:
        raise ControlError(f"the launch signing key file is unreadable: {error.strerror}") from None
    try:
        return LaunchKey(environ[KEY_ID_ENV], secret)
    except ValueError as error:
        raise ControlError(str(error)) from None


def _heartbeat(agent: AgentRecord, raw: str | None) -> list[Signal]:
    if not raw:
        return []
    beat = json.loads(raw)
    seen = beat["accepted_at_ms"] / 1000
    return [Signal(Source.HEARTBEAT, Reading.OK, seen, agent.execution_id, agent.generation, beat["state"])]


class ControlService:
    def __init__(
        self,
        store: RedisStore,
        slug: str,
        key: LaunchKey,
        authorize: Callable[[], bool],
        transports: Iterable[OperationTransport] = (),
        owner: str = "",
    ) -> None:
        self.controller = Controller(store, slug, transports, authorize)
        if owner:
            self.controller.owner = owner
        self.observer = Observer(store, BACKEND, Thresholds.from_environ(os.environ))
        self.grants = LaunchAuthority(store, key, ISSUER, AUDIENCE)
        self.tasks = TaskAuthority(store, self.controller, lambda token: self.grants.bound(slug, token))
        self.executions = ExecutionsAPI(self.grants, self.tasks)
        self.server: ThreadingHTTPServer | None = None
        self.listen: tuple[str, int] | None = None
        self.runtime = None
        self.api_url = ""

    def start(self) -> bool:
        if not self.controller.acquire():
            return False
        self._open()
        return True

    def tick(self, now: float | None = None) -> bool:
        if not (self.controller.renew() or self.controller.acquire()):
            return False
        self._open()
        store = self.controller.store
        self.observe(lease.now_ms(store) / 1000 if now is None else now)
        return True

    def observe(self, now: float) -> list[Finding]:
        store, slug = self.controller.store, self.controller.slug
        beats = store.redis.hgetall(store.key(slug, "heartbeats"))
        remote = [agent for agent in store.execution_occupants(slug).values() if agent.runtime_backend == BACKEND]
        found = [
            self.controller.observe(self.observer, agent, _heartbeat(agent, beats.get(agent.execution_id)), now)
            for agent in remote
        ]
        return [finding for finding in found if finding is not None]

    def _open(self) -> None:
        if self.listen is not None and self.server is None:
            self.serve(*self.listen)
            if self.api_url:
                self.controller.store.update(self.controller.slug, api_url=self.api_url)

    def serve(self, host: str, port: int) -> ThreadingHTTPServer:
        self.server = serve(self.executions, host, port)
        self.thread = threading.Thread(target=self.server.serve_forever, name=THREAD, daemon=True)
        self.thread.start()
        return self.server

    def stop(self) -> None:
        if self.server is not None:
            if self.thread.is_alive():
                self.server.shutdown()
            self.server.server_close()
        if self.controller.held is not None:
            self.controller.release()


def host(environ: Mapping[str, str], store: RedisStore, owner: str) -> ControlService | None:
    port = environ.get(PORT_ENV)
    if not port:
        return None
    slug = environ.get(SWARM_ENV)
    if not slug:
        raise ControlError(f"the control API needs {SWARM_ENV}")
    if not port.isdigit() or not 0 < int(port) <= MAX_PORT:
        raise ControlError(f"{PORT_ENV} must be a port number")
    credential = environ.get(CREDENTIAL_ENV)
    workers = deployed.Workers.from_environ(environ, slug)
    transports = () if workers is None else (workers.transport(environ, slug),)
    service = ControlService(
        store,
        slug,
        launch_key(environ),
        lambda: hive_auth.controller(store.redis, credential),
        transports,
        owner=owner,
    )
    service.listen = (HOST, int(port))
    if workers is not None:
        service.api_url = workers.terms.api_url
        service.runtime = deployed.tick_runtime(service, workers, environ)
    try:
        service.start()
    except BaseException:
        service.stop()
        raise
    if workers is not None:
        print(
            f"controller: built the Kubernetes runtime for {slug}: workers in {workers.policy['namespace']} "
            f"run {workers.policy['image_repository']}:{workers.image_tag} with profile {workers.profile}",
            flush=True,
        )
    return service
