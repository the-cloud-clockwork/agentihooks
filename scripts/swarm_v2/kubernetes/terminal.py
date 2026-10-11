import json
from collections.abc import Callable
from dataclasses import dataclass

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization

from scripts.swarm.store import RedisStore
from scripts.swarm_v2.auth_context import Registration
from scripts.swarm_v2.herdr import endpoints
from scripts.swarm_v2.herdr.attach import Terminal
from scripts.swarm_v2.herdr.capabilities import Verdict
from scripts.swarm_v2.herdr.endpoints import (
    Clients,
    Endpoint,
    EndpointRefused,
    HostIdentity,
    Scope,
    grant_scope,
    operator_scope,
)
from scripts.swarm_v2.kubernetes.client import AlreadyExists, ApiRefused, PodApi
from scripts.swarm_v2.kubernetes.spec import AdmittedLaunch, PodTemplate, Rendered, canonical_digest
from scripts.swarm_v2.runtime.commands import Principal

HOST_KEYS = "terminal-host-keys"


@dataclass(frozen=True)
class Exposure:
    clients: Clients
    user_ca: str

    def __post_init__(self) -> None:
        try:
            serialization.load_ssh_public_key(self.user_ca.encode())
        except (AttributeError, ValueError, UnsupportedAlgorithm):
            raise EndpointRefused("user_ca", "terminal user CA is not an OpenSSH public key") from None


class TerminalTemplate:
    def __init__(self, template: PodTemplate) -> None:
        self.template = template

    def render(self, record: object) -> Rendered:
        rendered = self.template.render(record)
        launch = AdmittedLaunch.from_record(record)
        scope = Scope("controller", launch.swarm_id, launch.execution_id, launch.generation)
        endpoint = Endpoint(scope, rendered.pod["metadata"]["namespace"])
        pod = endpoints.with_terminal(rendered.pod, endpoint, enabled=True)
        return Rendered(pod, canonical_digest(pod))


@dataclass(frozen=True)
class Launched:
    execution_id: str
    generation: int
    host_public: str


class HostKeys:
    def __init__(self, store: RedisStore) -> None:
        self.store = store

    def record(self, scope: Scope, host_public: str) -> None:
        entry = {"generation": scope.generation, "host_public": host_public}
        self.store.redis.hset(self.store.key(scope.swarm, HOST_KEYS), scope.execution_id, json.dumps(entry))

    def launched(self, swarm: str, execution_id: str) -> Launched:
        raw = self.store.redis.hget(self.store.key(swarm, HOST_KEYS), execution_id)
        if not raw:
            raise EndpointRefused("host_key_unrecorded", f"no host key was recorded at launch for {execution_id}")
        entry = json.loads(raw)
        return Launched(execution_id, entry["generation"], entry["host_public"])


class TerminalObjects:
    def __init__(
        self,
        api: PodApi,
        exposure: Exposure,
        keys: HostKeys,
        identity: Callable[[], HostIdentity] = HostIdentity.generate,
    ) -> None:
        self.api, self.exposure, self.keys, self.identity = api, exposure, keys, identity

    def open(self, registration: Registration, pod: dict) -> str:
        from redis.exceptions import RedisError

        made = self.identity()
        try:
            scope = grant_scope(registration)
            endpoint = Endpoint(scope, self.api.namespace)
            exposure = self.exposure
            for manifest in endpoints.objects(endpoint, pod, made, exposure.user_ca, exposure.clients, enabled=True):
                self.api.create_object(manifest)
            self.keys.record(scope, made.public)
        except EndpointRefused as refused:
            return f"terminal_{refused.reason}"
        except AlreadyExists:
            return "terminal_exists"
        except ApiRefused:
            return "terminal_refused"
        except (OSError, RedisError):
            return "terminal_unavailable"
        return ""


def operator_attach(terminal: Terminal, keys: HostKeys, principal: Principal, swarm: str, execution_id: str) -> Verdict:
    launched = keys.launched(swarm, execution_id)
    return terminal.attach(operator_scope(principal, swarm, launched), launched.host_public)
