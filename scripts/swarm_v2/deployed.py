import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from scripts.swarm.store import RedisStore, SwarmError
from scripts.swarm_v2 import image_tag
from scripts.swarm_v2.accounts import AccountCapacity, fleet_held
from scripts.swarm_v2.auth_context import Registration
from scripts.swarm_v2.herdr.capabilities import Qualifier, machine_probe
from scripts.swarm_v2.herdr.endpoints import Clients
from scripts.swarm_v2.herdr.target import DELIVERIES, Router
from scripts.swarm_v2.herdr.viewer import Viewer
from scripts.swarm_v2.kubernetes.adapter import KubernetesRuntime
from scripts.swarm_v2.kubernetes.approved import INDEX, AccountRoute, FleetAccounts, SecretKeys
from scripts.swarm_v2.kubernetes.cleanup import Cleanup
from scripts.swarm_v2.kubernetes.client import ApiRefused, KubeHttp, PodApi, PodClient
from scripts.swarm_v2.kubernetes.completion import Completion
from scripts.swarm_v2.kubernetes.failures import AccountSlot, Recovery
from scripts.swarm_v2.kubernetes.grants import PodGrants, Supervision
from scripts.swarm_v2.kubernetes.pool import WorkerPool
from scripts.swarm_v2.kubernetes.prompts import TerminalPrompts
from scripts.swarm_v2.kubernetes.replacement import Replacements, TaskLedger
from scripts.swarm_v2.kubernetes.runtime import KubernetesTransport
from scripts.swarm_v2.kubernetes.spec import PodSpecRefused, PodTemplate, load_policy
from scripts.swarm_v2.kubernetes.terminal import Exposure, HostKeys, TerminalObjects, TerminalTemplate
from scripts.swarm_v2.kubernetes.terminal_views import TerminalViews
from scripts.swarm_v2.kubernetes.watch import OWNER_LABEL, CursorExpired, owner_for
from scripts.swarm_v2.launch import DistributedLaunch, LaunchTerms
from scripts.swarm_v2.operator_auth import OPERATOR
from scripts.swarm_v2.quota import QuotaObservations
from scripts.swarm_v2.registry import FleetRegistry
from scripts.swarm_v2.runtime.base import SpawnRequest
from scripts.swarm_v2.runtime.routed import RoutedRuntime, routed

if TYPE_CHECKING:
    from scripts.swarm_v2.auth_context import LaunchAuthority
    from scripts.swarm_v2.control_service import ControlService
    from scripts.swarm_v2.controller import Controller

API_URL_ENV = "AGENTIHOOKS_CONTROL_API_URL"
POLICY_ENV = "AGENTIHOOKS_POD_POLICY_FILE"
IMAGE_TAG_ENV = "AGENTIHOOKS_WORKER_IMAGE_TAG"
PROFILE_ENV = "AGENTIHOOKS_WORKER_PROFILE"
CAP_ENV = "AGENTIHOOKS_LAUNCH_CAP"
PROJECTS_ENV = "AGENTIHOOKS_LAUNCH_PROJECTS"
BRAIN_ENV = "AGENTIHOOKS_LAUNCH_BRAIN"
EXPORTER_ENV = "AGENTIHOOKS_WORKER_EXPORTER"
DEFAULT_EXPORTER = ("python", "-m", "scripts.swarm_v2.session_export")
TERMINAL_ENV = "AGENTIHOOKS_WORKER_TERMINAL"
AGGREGATION_ENV = "AGENTIHOOKS_TERMINAL_AGGREGATION"
REQUIRED = (POLICY_ENV, IMAGE_TAG_ENV, PROFILE_ENV, CAP_ENV, PROJECTS_ENV, BRAIN_ENV)
RESERVATION_MS = 300_000
HARNESS = "claude"


class WorkerSettingsRefused(SwarmError):
    pass


def pod_api(environ: Mapping[str, str], namespace: str) -> PodApi:
    try:
        http = KubeHttp.in_cluster(environ)
    except KeyError as missing:
        raise WorkerSettingsRefused(f"the Kubernetes runtime needs {missing.args[0]}") from None
    except OSError as error:
        raise WorkerSettingsRefused(f"the service account CA is unreadable: {error.strerror}") from None
    return PodClient(http, namespace)


def pod_policy(path: str) -> dict:
    try:
        policy = load_policy(path)
    except PodSpecRefused as refused:
        raise WorkerSettingsRefused(str(refused)) from None
    if policy["provider_accounts"]:
        raise WorkerSettingsRefused(
            f"the Pod policy must name no provider account: the approved accounts are the keys of Secret {INDEX}"
        )
    return policy


def _exporter(value: str | None) -> tuple[str, ...]:
    if not value:
        return DEFAULT_EXPORTER
    refused = WorkerSettingsRefused(f"{EXPORTER_ENV} must be a JSON list of command words")
    try:
        words = json.loads(value)
    except ValueError:
        raise refused from None
    if not isinstance(words, list) or not words or not all(isinstance(w, str) and w and "\0" not in w for w in words):
        raise refused
    return tuple(words)


def _terminal(value: str | None) -> Exposure | None:
    if not value:
        return None
    try:
        setting = json.loads(value)
        return Exposure(Clients(**setting["clients"]), setting["user_ca"])
    except (ValueError, TypeError, KeyError, AttributeError):
        raise WorkerSettingsRefused(
            f"{TERMINAL_ENV} must be a JSON object naming the terminal clients and user CA"
        ) from None


@dataclass(frozen=True)
class Workers:
    terms: LaunchTerms
    policy: dict
    image_tag: str
    profile: str
    exporter: tuple[str, ...]
    terminal: Exposure | None

    @classmethod
    def from_environ(cls, environ: Mapping[str, str], slug: str) -> "Workers | None":
        api_url = environ.get(API_URL_ENV)
        if not api_url:
            return None
        if missing := [name for name in REQUIRED if not environ.get(name)]:
            raise WorkerSettingsRefused(f"the Kubernetes runtime needs {', '.join(missing)}")
        cap = environ[CAP_ENV]
        if not (cap.isascii() and cap.isdigit()) or int(cap) < 1:
            raise WorkerSettingsRefused(f"{CAP_ENV} must be a whole number above zero")
        if not image_tag.is_tag(environ[IMAGE_TAG_ENV]):
            raise WorkerSettingsRefused(f"{IMAGE_TAG_ENV} must be an image tag, never a digest")
        policy, profile = pod_policy(environ[POLICY_ENV]), environ[PROFILE_ENV]
        if profile not in policy["profiles"]:
            raise WorkerSettingsRefused(f"the Pod policy has no {profile} profile")
        projects = tuple(project.strip() for project in environ[PROJECTS_ENV].split(",") if project.strip())
        if not projects:
            raise WorkerSettingsRefused(f"{PROJECTS_ENV} names no project")
        terms = LaunchTerms("", int(cap), RESERVATION_MS, projects, environ[BRAIN_ENV], api_url, "bundle" in policy)
        exporter, terminal = _exporter(environ.get(EXPORTER_ENV)), _terminal(environ.get(TERMINAL_ENV))
        return cls(terms, policy, environ[IMAGE_TAG_ENV], profile, exporter, terminal)

    @property
    def requested(self) -> dict:
        profile = self.policy["profiles"][self.profile]
        cpu = profile.get("requests", profile["limits"])["cpu_millis"]
        return {"memory_mib": profile["limits"]["memory_mib"], "cpu_millis": cpu}

    def launch(self, request: SpawnRequest) -> dict:
        task, shape = request.task, self.requested
        return {
            "task_id": task["id"],
            "swarm_id": request.config.slug,
            "seat_id": task["seat"],
            "grant_ref": f"grant-{task['execution_id']}",
            "controller_epoch": task["controller_epoch"],
            "project_id": self.terms.project_ids[0],
            "harness": HARNESS,
            "image_digest": image_tag.resolve(self.policy["image_repository"], self.image_tag),
            "profile": self.profile,
            "memory_mib": shape["memory_mib"],
            "cpu_millis": shape["cpu_millis"],
            "provider_account": task["account"],
            "task_payload": {
                "task_id": task["id"],
                "lane": request.lane,
                "name": request.name,
                "profile": task["profile"],
                "endpoints": task["endpoints"],
                **({"checkpoint": task["checkpoint"]} if task.get("checkpoint") else {}),
            },
        }

    def target(self, request: SpawnRequest) -> dict:
        return {"pod_namespace": self.policy["namespace"], "pod_name": f"swarm-{request.task['id']}"}

    def transport(self, environ: Mapping[str, str], slug: str) -> KubernetesTransport:
        api = pod_api(environ, self.policy["namespace"])
        template = PodTemplate(self.policy, owner_for(slug), SecretKeys(api))
        mounted = template if self.terminal is None else TerminalTemplate(template)
        return KubernetesTransport(api, slug, mounted)

    def grants(
        self, environ: Mapping[str, str], slug: str, verify: Callable[[str], Registration], store: RedisStore
    ) -> PodGrants:
        supervision = Supervision(HARNESS, self.exporter, self.terms.api_url)
        api = pod_api(environ, self.policy["namespace"])
        terminal = None if self.terminal is None else TerminalObjects(api, self.terminal, HostKeys(store))
        return PodGrants(api, slug, verify, supervision, terminal)


def tick_runtime(service: "ControlService", workers: Workers, environ: Mapping[str, str]) -> RoutedRuntime:
    controller, grants = service.controller, service.grants
    slug = controller.slug

    def verify(token: str):
        return grants.verify(slug, token)

    capacity, fleet = AccountCapacity(controller.store, slug, verify), FleetRegistry(controller.store, slug, verify)
    launcher = DistributedLaunch(
        controller, grants, capacity, fleet, None, workers.grants(environ, slug, verify, controller.store)
    )
    api = pod_api(environ, workers.policy["namespace"])
    keys = SecretKeys(api)
    route = AccountRoute(keys, QuotaObservations(controller.store, slug, verify), partial(_held, capacity), HARNESS)
    launcher.accounts = route.choose
    prompts = None if controller.terminals is None else TerminalPrompts(controller.terminals.terminal.router())
    kubernetes = KubernetesRuntime(controller.execute, workers.launch, prompts)
    runtime = routed(
        environ, kubernetes=kubernetes, launch=partial(launcher.from_tick, terms=workers.terms, target=workers.target)
    )
    runtime.herdr_runtime.quota_accounts = FleetAccounts(route, workers.terms.cap)
    runtime.herdr_runtime.host = WorkerPool(api, workers.policy.get("worker_pool"), workers.requested)
    launcher.planned = partial(runtime.herdr_runtime.planned_account, agent=HARNESS)
    launcher.router = runtime.router
    runtime.relaunch = partial(launcher.relaunch, terms=workers.terms)
    return runtime


def terminal_views(controller: "Controller", environ: Mapping[str, str]) -> TerminalViews:
    terminal = controller.terminals.terminal
    registrations, journal = terminal.registrations, terminal.home.terminals / DELIVERIES
    qualifier = Qualifier(machine_probe(terminal.run))
    router = Router(registrations.store, registrations.catalog, qualifier, terminal.run, journal, mutations=False)
    aggregation = environ.get(AGGREGATION_ENV) != "off"
    viewer = Viewer(router, terminal.run, OPERATOR, aggregation=aggregation)
    return TerminalViews(controller.store, controller.slug, viewer)


def _held(capacity: AccountCapacity) -> dict[str, int]:
    return fleet_held(capacity.store, capacity.clock())


class GrantRelease:
    def __init__(self, grants: "LaunchAuthority", slug: str) -> None:
        self.grants, self.slug = grants, slug

    def release(self, execution_id: str) -> None:
        self.grants.revoke(self.slug, execution_id)


class AttachRelease:
    """Herdr upkeep attaches only to attempts with a recorded host key, so forgetting the key releases the attach."""

    def __init__(self, keys: HostKeys, slug: str) -> None:
        self.keys, self.slug = keys, slug

    def release(self, execution_id: str) -> None:
        self.keys.forget(self.slug, execution_id)


class NoCheckpoints:
    """No checkpoint store is deployed yet, so every failure waits for an explicit fresh or resume decision."""

    def list(self, execution_id: str) -> list[dict]:
        return []


class RecoveryPass:
    """Each pass lists first, then watches from the previous listing's version, so consecutive watches overlap and
    leave no gap; when the watch fails, Pods missing since the last listing stand in for its deletions."""

    def __init__(self, recovery: Recovery, api: PodApi, selector: str) -> None:
        self.recovery, self.api, self.selector = recovery, api, selector
        self.version, self.listed = "", {}
        self.replacements: Replacements | None = None
        self.completion: Completion | None = None

    def __call__(self) -> dict[str, str]:
        pods, version = self.api.snapshot(self.selector)
        gone = self._deleted()
        self.version = version
        live = {_uid(pod) for pod in pods}
        for uid, pod in self.listed.items():
            if uid not in live:
                gone.setdefault(uid, pod)
        self.listed = {_uid(pod): pod for pod in pods}
        seen = self.recovery.reconcile(pods, self.api.ready_nodes(), list(gone.values()))
        if self.replacements is not None:
            self.replacements()
        if self.completion is not None:
            self.completion(pods)
        return seen

    def _deleted(self) -> dict[str, dict]:
        if not self.version:
            return {}
        try:
            events = self.api.watch_pods(self.selector, self.version)
        except (CursorExpired, ApiRefused, ConnectionError, TimeoutError) as error:
            print(f"controller: the Pod watch failed with {type(error).__name__}; using the last listing", flush=True)
            return {}
        return {_uid(pod): pod for kind, pod in events if kind == "DELETED"}


def _uid(pod: dict) -> str:
    return pod["metadata"]["uid"]


def recovery_pass(
    service: "ControlService", workers: Workers, api: PodApi, ledger: TaskLedger | None = None
) -> RecoveryPass:
    controller, grants = service.controller, service.grants
    slug = controller.slug

    def verify(token: str):
        return grants.verify(slug, token)

    capacity = AccountCapacity(controller.store, slug, verify)
    releases = {"grant": GrantRelease(grants, slug), "account": AccountSlot(controller.store, slug, capacity)}
    recovery = Recovery(controller.store, slug, controller, NoCheckpoints(), releases, workers.image_tag)
    recovering = RecoveryPass(recovery, api, f"{OWNER_LABEL}={owner_for(slug)}")
    attach = AttachRelease(HostKeys(controller.store), slug)
    cleanup = Cleanup(controller.store, slug, api, controller.require, attach, releases["grant"])
    recovering.completion = Completion(controller.store, slug, cleanup)
    if ledger is not None:
        recovering.replacements = Replacements(recovery, service.runtime.relaunch, ledger)
    return recovering
