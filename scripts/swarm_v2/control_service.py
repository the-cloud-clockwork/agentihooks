import json
import os
import threading
from collections.abc import Callable, Iterable, Mapping
from http.server import ThreadingHTTPServer
from pathlib import Path

from scripts.hive import auth as hive_auth
from scripts.swarm import lease, ledger_host
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import AgentRecord, RedisStore, SwarmError
from scripts.swarm_ledger import ledger_link
from scripts.swarm_ledger.repository.sqlite import DATABASE, SQLiteLedgerRepository
from scripts.swarm_v2 import deployed, ledger_writer, operator_auth, telemetry
from scripts.swarm_v2.api import artifacts, health
from scripts.swarm_v2.api.executions import ExecutionsAPI
from scripts.swarm_v2.api.recovery import RecoveryAPI
from scripts.swarm_v2.api.server import Routes, serve
from scripts.swarm_v2.api.tasks import TasksAPI
from scripts.swarm_v2.artifacts import remote
from scripts.swarm_v2.auth_context import LaunchAuthority, LaunchKey
from scripts.swarm_v2.authority import TaskAuthority
from scripts.swarm_v2.controller import Controller
from scripts.swarm_v2.herdr.attach import ClientHome
from scripts.swarm_v2.herdr.capabilities import runner
from scripts.swarm_v2.herdr.upkeep import Upkeep, switched_on
from scripts.swarm_v2.hosted_ledger import HostedLedger
from scripts.swarm_v2.kubernetes import operator_terminal
from scripts.swarm_v2.kubernetes.client import ApiRefused
from scripts.swarm_v2.kubernetes.runtime import PodStatus
from scripts.swarm_v2.kubernetes.spec import pod_name
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
VIEWS_THREAD = "swarm-v2-terminal-views"
ISSUER = "controller"
AUDIENCE = "workers"
REFUSED = {401: Reading.UNAUTHENTICATED, 403: Reading.FORBIDDEN}


class ControlError(SwarmError):
    pass


def page_credential(slug: str) -> str | None:
    from scripts.swarm_ledger.repository import repository

    return repository.token(slug)


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


def _pod(status: Callable[[str], tuple[PodStatus, ...]] | None, agent: AgentRecord, now: float) -> list[Signal]:
    if status is None:
        return []
    try:
        found = [pod for pod in status(agent.execution_id) if pod.name == pod_name(agent.execution_id)]
    except ApiRefused as refused:
        reading, phase = REFUSED.get(refused.status, Reading.UNREACHABLE), ""
    except (ConnectionError, TimeoutError):
        reading, phase = Reading.UNREACHABLE, ""
    else:
        reading, phase = (Reading.OK, found[0].phase) if found else (Reading.NOT_FOUND, "")
    return [Signal(Source.KUBERNETES, reading, now, agent.execution_id, agent.generation, phase)]


class ControlService:
    def __init__(
        self,
        store: RedisStore,
        slug: str,
        key: LaunchKey,
        authorize: Callable[[], bool],
        transports: Iterable[OperationTransport] = (),
        owner: str = "",
        operator: Callable[[str, str], object] | None = None,
    ) -> None:
        self.controller = Controller(store, slug, transports, authorize)
        self.controller.telemetry = telemetry.Telemetry(store, slug, telemetry.configured_exporter(os.environ))
        if owner:
            self.controller.owner = owner
        self.observer = Observer(store, BACKEND, Thresholds.from_environ(os.environ))
        self.grants = LaunchAuthority(store, key, ISSUER, AUDIENCE)
        self.tasks = TaskAuthority(store, self.controller, lambda token: self.grants.bound(slug, token))
        self.executions = ExecutionsAPI(self.grants, self.tasks)
        backend = remote.controller_backend(os.environ)
        self.artifacts = None if backend is None else artifacts.ArtifactsAPI(backend, self.tasks)
        self.health = health.Health(store, slug, health.brain_probe(os.environ), self.controller)
        self.health_api = health.HealthAPI(self.health, self.grants, operator, health.deep_enabled())
        self.server: ThreadingHTTPServer | None = None
        self.listen: tuple[str, int] | None = None
        self.runtime = None
        self.pods: Callable[[str], tuple[PodStatus, ...]] | None = None
        self.api_url = ""
        self.recover: Callable[[], dict[str, str]] | None = None
        self.recovery_api = RecoveryAPI(
            store,
            slug,
            lambda: getattr(self.recover, "recovery", None),
            operator,
            lambda token: self.grants.bound(slug, token),
        )
        self.views: Callable[[], dict] | None = None
        self.viewing: threading.Thread | None = None
        self.tasks_api: TasksAPI | None = None
        self.writer: ledger_writer.WriterLease | None = None
        self.ledger_folder: Path | None = None
        self.hosted_ledger: HostedLedger | None = None

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
        if self.recover is not None:
            self.recover()
        self._view()
        return True

    def _view(self) -> None:
        if self.views is None or (self.viewing is not None and self.viewing.is_alive()):
            return
        self.viewing = threading.Thread(target=self.views, name=VIEWS_THREAD, daemon=True)
        self.viewing.start()

    def observe(self, now: float) -> list[Finding]:
        store, slug = self.controller.store, self.controller.slug
        beats = store.redis.hgetall(store.key(slug, "heartbeats"))
        remote = [agent for agent in store.execution_occupants(slug).values() if agent.runtime_backend == BACKEND]
        found = [
            self.controller.observe(
                self.observer,
                agent,
                _heartbeat(agent, beats.get(agent.execution_id)) + _pod(self.pods, agent, now),
                now,
            )
            for agent in remote
        ]
        return [finding for finding in found if finding is not None]

    def _open(self) -> None:
        store, slug = self.controller.store, self.controller.slug
        if self.listen is not None and self.server is None:
            if self.hosted_ledger is not None and self.tasks_api is None:
                self.tasks_api = TasksAPI(self.grants, self.tasks, self.hosted_ledger)
            if self.ledger_folder is not None and self.tasks_api is None:
                self.mount_ledger(self.ledger_folder)
            self.serve(*self.listen)
            if self.api_url and slug not in store.slugs():
                print(
                    f"controller: serves the control API for {slug}, which does not exist yet; waits for it", flush=True
                )
        if self.server is not None and self.api_url and slug in store.slugs():
            if store.config(slug).api_url != self.api_url:
                store.update(slug, api_url=self.api_url)

    def serve(self, host: str, port: int) -> ThreadingHTTPServer:
        routes = Routes(self.executions, self.tasks_api, health=self.health_api, recovery=self.recovery_api)
        routes.artifacts = self.artifacts
        self.server = serve(routes, host, port)
        self.thread = threading.Thread(target=self.server.serve_forever, name=THREAD, daemon=True)
        self.thread.start()
        return self.server

    def mount_ledger(self, folder: Path) -> None:
        self.writer = ledger_writer.WriterLease(folder)
        try:
            self.writer.acquire(ledger_writer.owner())
        except ledger_writer.WriterConflict as conflict:
            print(f"controller: serves the tasks API read only for {self.controller.slug}: {conflict}", flush=True)
        ledger = SQLiteLedgerRepository(folder / DATABASE)
        self.tasks_api = TasksAPI(self.grants, self.tasks, ledger, self.writer.held)

    def stop(self) -> None:
        if self.server is not None:
            if self.thread.is_alive():
                self.server.shutdown()
            self.server.server_close()
        if self.writer is not None:
            self.writer.release()
        if self.controller.held is not None:
            self.controller.release()


def anton_client(environ: Mapping[str, str], slug: str, workers: deployed.Workers | None) -> Upkeep | None:
    if workers is None or workers.terminal is None or not switched_on(environ):
        return None
    if not environ.get(operator_terminal.CA_KEY_ENV):
        print(
            f"controller: keeps no saved herdr machines for {slug}: {operator_terminal.CA_KEY_ENV} is unset", flush=True
        )
        return None
    home = ClientHome(Path.home())
    return Upkeep(operator_terminal.terminal(environ, home, runner(home.environment(dict(environ)))))


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
    hosted = ledger_link.remote(environ)
    if hosted and not credential:
        raise ControlError(f"the hosted ledger needs {CREDENTIAL_ENV}")
    workers = deployed.Workers.from_environ(environ, slug)
    transports = () if workers is None else (workers.transport(environ, slug),)
    service = ControlService(
        store,
        slug,
        launch_key(environ),
        lambda: hive_auth.controller(store.redis, credential),
        transports,
        owner=owner,
        operator=operator_auth.authenticator(page_credential),
    )
    service.controller.terminals = anton_client(environ, slug, workers)
    service.listen = (HOST, int(port))
    if workers is not None:
        service.api_url = workers.terms.api_url
        service.runtime = deployed.tick_runtime(service, workers, environ)
        service.pods = transports[0].status
        service.recover = deployed.recovery_pass(service, workers, transports[0].api, LedgerClient(), environ)
        if service.controller.terminals is not None:
            service.views = deployed.terminal_views(service.controller, environ)
    if hosted:
        service.hosted_ledger = HostedLedger(ledger_link.base(environ), credential, store)
    else:
        service.ledger_folder = ledger_host.folder(environ)
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
