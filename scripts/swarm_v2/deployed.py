"""The deployed controller's tick runtime: engineer and CI spawns go to Kubernetes through the distributed launch."""

from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial

from scripts.swarm.store import AgentRecord, SwarmError
from scripts.swarm_v2.accounts import AccountCapacity
from scripts.swarm_v2.kubernetes.adapter import KubernetesRuntime
from scripts.swarm_v2.kubernetes.client import KubeHttp, PodApi, PodClient
from scripts.swarm_v2.kubernetes.runtime import KubernetesTransport
from scripts.swarm_v2.kubernetes.spec import PodSpecRefused, PodTemplate, load_policy
from scripts.swarm_v2.kubernetes.watch import owner_for
from scripts.swarm_v2.launch import DistributedLaunch, LaunchTerms
from scripts.swarm_v2.registry import FleetRegistry
from scripts.swarm_v2.runtime.base import SpawnRequest
from scripts.swarm_v2.runtime.routed import RoutedRuntime, routed

API_URL_ENV = "AGENTIHOOKS_CONTROL_API_URL"
POLICY_ENV = "AGENTIHOOKS_POD_POLICY_FILE"
IMAGE_ENV = "AGENTIHOOKS_WORKER_IMAGE_DIGEST"
PROFILE_ENV = "AGENTIHOOKS_WORKER_PROFILE"
ACCOUNT_ENV = "AGENTIHOOKS_LAUNCH_ACCOUNT"
CAP_ENV = "AGENTIHOOKS_LAUNCH_CAP"
PROJECTS_ENV = "AGENTIHOOKS_LAUNCH_PROJECTS"
BRAIN_ENV = "AGENTIHOOKS_LAUNCH_BRAIN"
REQUIRED = (POLICY_ENV, IMAGE_ENV, PROFILE_ENV, ACCOUNT_ENV, CAP_ENV, PROJECTS_ENV, BRAIN_ENV)
RESERVATION_MS = 300_000
HARNESS = "claude"


class WorkerSettingsRefused(SwarmError):
    pass


class PodGrants:
    """No path hands a launch grant to a worker Pod yet, so the launch records it as not handed."""

    def hand(self, agent: AgentRecord, grant: str) -> bool:
        return False


def pod_api(environ: Mapping[str, str], namespace: str) -> PodApi:
    return PodClient(KubeHttp.in_cluster(environ), namespace)


def _policy(path: str, slug: str) -> dict:
    try:
        policy = load_policy(path)
    except PodSpecRefused as refused:
        raise WorkerSettingsRefused(str(refused)) from None
    if policy["owner"] != owner_for(slug):
        raise WorkerSettingsRefused(f"the Pod policy owner must be {owner_for(slug)}")
    return policy


@dataclass(frozen=True)
class Workers:
    terms: LaunchTerms
    policy: dict
    image_digest: str
    profile: str

    @classmethod
    def from_environ(cls, environ: Mapping[str, str], slug: str) -> "Workers | None":
        api_url = environ.get(API_URL_ENV)
        if not api_url:
            return None
        if missing := [name for name in REQUIRED if not environ.get(name)]:
            raise WorkerSettingsRefused(f"the Kubernetes runtime needs {', '.join(missing)}")
        cap = environ[CAP_ENV]
        if not cap.isdigit() or int(cap) < 1:
            raise WorkerSettingsRefused(f"{CAP_ENV} must be a whole number above zero")
        policy, profile = _policy(environ[POLICY_ENV], slug), environ[PROFILE_ENV]
        if profile not in policy["profiles"]:
            raise WorkerSettingsRefused(f"the Pod policy has no {profile} profile")
        projects = tuple(project.strip() for project in environ[PROJECTS_ENV].split(",") if project.strip())
        terms = LaunchTerms(environ[ACCOUNT_ENV], int(cap), RESERVATION_MS, projects, environ[BRAIN_ENV], api_url)
        return cls(terms, policy, environ[IMAGE_ENV], profile)

    def launch(self, request: SpawnRequest) -> dict:
        task, limits = request.task, self.policy["profiles"][self.profile]["limits"]
        return {
            "task_id": task["id"],
            "swarm_id": request.config.slug,
            "seat_id": task["seat"],
            "grant_ref": f"grant-{task['execution_id']}",
            "controller_epoch": task["controller_epoch"],
            "project_id": self.terms.project_ids[0],
            "harness": HARNESS,
            "image_digest": self.image_digest,
            "profile": self.profile,
            "memory_mib": limits["memory_mib"],
            "cpu_millis": limits["cpu_millis"],
            "provider_account": self.terms.account,
            "task_payload": {
                "task_id": task["id"],
                "lane": request.lane,
                "name": request.name,
                "endpoints": task["endpoints"],
            },
        }

    def transport(self, environ: Mapping[str, str], slug: str) -> KubernetesTransport:
        return KubernetesTransport(pod_api(environ, self.policy["namespace"]), slug, PodTemplate(self.policy))


def tick_runtime(service, workers: Workers, environ: Mapping[str, str]) -> RoutedRuntime:
    controller, grants = service.controller, service.grants
    slug = controller.slug

    def verify(token: str):
        return grants.verify(slug, token)

    capacity, fleet = AccountCapacity(controller.store, slug, verify), FleetRegistry(controller.store, slug, verify)
    launcher = DistributedLaunch(controller, grants, capacity, fleet, None, PodGrants())
    kubernetes = KubernetesRuntime(controller.execute, workers.launch)
    runtime = routed(environ, kubernetes=kubernetes, launch=partial(launcher.from_tick, terms=workers.terms))
    launcher.router = runtime.router
    return runtime
