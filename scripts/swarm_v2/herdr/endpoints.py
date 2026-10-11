import base64
import copy
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.swarm_v2.auth_context import Registration
from scripts.swarm_v2.kubernetes.runtime import GENERATION_LABEL
from scripts.swarm_v2.kubernetes.spec import DOMAIN, IDENTITY, LABEL_VALUE, pod_name
from scripts.swarm_v2.kubernetes.watch import EXECUTION_LABEL, OWNER_LABEL, owner_for
from scripts.swarm_v2.runtime.commands import Principal, Role

PORT = 2222
PORT_NAME = "herdr-ssh"
MATERIAL_DIR = "/var/run/swarm/terminal"
HOST_KEY = "host_key"
USER_CA = "user_ca.pub"
PRINCIPALS = "principals"
VOLUME = "terminal"
CLUSTER_DOMAIN = "svc.cluster.local"
COMPONENT_LABEL = f"{DOMAIN}/component"
COMPONENT = "swarm-terminal"
CERTIFICATE_SECONDS = 300
CLOCK_SKEW_SECONDS = 30
NAMESPACE = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
PUBLIC_SERVICE_FIELDS = ("externalIPs", "externalName", "loadBalancerIP", "loadBalancerSourceRanges")


class EndpointRefused(ValueError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Scope:
    actor: str
    swarm: str
    execution_id: str
    generation: int


def _scope(actor: str, swarm: object, execution_id: object, generation: object) -> Scope:
    if not isinstance(swarm, str) or not LABEL_VALUE.fullmatch(swarm):
        raise EndpointRefused("scope", "terminal scope swarm is not a label value")
    if not isinstance(execution_id, str) or not IDENTITY["execution_id"].fullmatch(execution_id):
        raise EndpointRefused("scope", "terminal scope execution is not an execution id")
    if type(generation) is not int or generation < 1:
        raise EndpointRefused("scope", "terminal scope generation is not a positive integer")
    return Scope(actor, swarm, execution_id, generation)


def grant_scope(registration: Registration) -> Scope:
    actor = f"grant/{registration.grant_id}"
    return _scope(actor, registration.swarm_id, registration.execution_id, registration.generation)


def operator_scope(principal: Principal, swarm: str, execution) -> Scope:
    if principal.role is not Role.OPERATOR:
        raise EndpointRefused("unauthorized", "only a launch grant or the operator role opens a worker terminal")
    return _scope(f"operator/{principal.name}", swarm, execution.execution_id, execution.generation)


@dataclass(frozen=True)
class Clients:
    namespace: str
    labels: dict[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, str) or not NAMESPACE.fullmatch(self.namespace):
            raise EndpointRefused("clients", "terminal client namespace is not a namespace name")
        if not self.labels or not all(isinstance(v, str) and v for v in self.labels.values()):
            raise EndpointRefused("clients", "terminal clients must be selected by at least one label")


@dataclass(frozen=True)
class Endpoint:
    scope: Scope
    namespace: str

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, str) or not NAMESPACE.fullmatch(self.namespace):
            raise EndpointRefused("namespace", "terminal namespace is not a namespace name")

    @property
    def name(self) -> str:
        return f"{pod_name(self.scope.execution_id)}-terminal"

    @property
    def host(self) -> str:
        return f"{self.name}.{self.namespace}.{CLUSTER_DOMAIN}"

    @property
    def target(self) -> str:
        return f"ssh://{self.host}"

    @property
    def incarnation(self) -> str:
        return self.scope.execution_id

    @property
    def labels(self) -> dict[str, str]:
        scope = self.scope
        return {
            OWNER_LABEL: owner_for(scope.swarm),
            EXECUTION_LABEL: scope.execution_id,
            GENERATION_LABEL: str(scope.generation),
        }


def _encode(text: str | bytes) -> str:
    return base64.b64encode(text if isinstance(text, bytes) else text.encode()).decode()


def _public(key) -> str:
    return key.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()


@dataclass(frozen=True)
class HostIdentity:
    private: bytes = field(repr=False)
    public: str

    @classmethod
    def generate(cls) -> "HostIdentity":
        key = Ed25519PrivateKey.generate()
        private = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()
        )
        return cls(private, _public(key.public_key()))

    def known_hosts(self, endpoint: Endpoint) -> str:
        return f"{endpoint.host} {self.public}\n"


class TerminalAuthority:
    def __init__(
        self, key: Ed25519PrivateKey, clock: Callable[[], float] = time.time, seconds: int = CERTIFICATE_SECONDS
    ) -> None:
        self.key, self.clock, self.seconds = key, clock, seconds

    @property
    def user_ca(self) -> str:
        return _public(self.key.public_key())

    def issue(self, scope: Scope, client_public: str) -> str:
        try:
            key = serialization.load_ssh_public_key(client_public.encode())
        except (ValueError, UnsupportedAlgorithm):
            raise EndpointRefused("client_key", "terminal client key is not an OpenSSH public key") from None
        now = int(self.clock())
        certificate = (
            serialization.SSHCertificateBuilder()
            .public_key(key)
            .serial(secrets.randbits(64))
            .type(serialization.SSHCertificateType.USER)
            .key_id(scope.actor.encode())
            .valid_principals([scope.execution_id.encode()])
            .valid_after(now - CLOCK_SKEW_SECONDS)
            .valid_before(now + self.seconds)
            .sign(self.key)
        )
        return certificate.public_bytes().decode() + "\n"


def _owned(endpoint: Endpoint, pod: dict) -> dict:
    metadata = pod.get("metadata", {})
    found = metadata.get("labels", {})
    if (
        metadata.get("name") != pod_name(endpoint.scope.execution_id)
        or metadata.get("namespace") != endpoint.namespace
        or "deletionTimestamp" in metadata
        or {key: found.get(key) for key in endpoint.labels} != endpoint.labels
    ):
        raise EndpointRefused("pod_foreign", "the Pod is not this execution's live attempt")
    return {"apiVersion": "v1", "kind": "Pod", "name": metadata["name"], "uid": metadata["uid"]}


def _metadata(endpoint: Endpoint, owner: dict) -> dict:
    return {
        "name": endpoint.name,
        "namespace": endpoint.namespace,
        "labels": {**endpoint.labels, COMPONENT_LABEL: COMPONENT},
        "ownerReferences": [owner],
    }


def host_secret(endpoint: Endpoint, owner: dict, identity: HostIdentity, user_ca: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": _metadata(endpoint, owner),
        "type": "Opaque",
        "immutable": True,
        "data": {
            HOST_KEY: _encode(identity.private),
            USER_CA: _encode(f"{user_ca}\n"),
            PRINCIPALS: _encode(f"{endpoint.scope.execution_id}\n"),
        },
    }


def service(endpoint: Endpoint, owner: dict) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": _metadata(endpoint, owner),
        "spec": {
            "type": "ClusterIP",
            "selector": endpoint.labels,
            "ports": [{"name": PORT_NAME, "protocol": "TCP", "port": PORT, "targetPort": PORT}],
        },
    }


def network_policy(endpoint: Endpoint, owner: dict, clients: Clients) -> dict:
    peer = {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": clients.namespace}},
        "podSelector": {"matchLabels": dict(clients.labels)},
    }
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": _metadata(endpoint, owner),
        "spec": {
            "podSelector": {"matchLabels": endpoint.labels},
            "policyTypes": ["Ingress"],
            "ingress": [{"from": [peer], "ports": [{"protocol": "TCP", "port": PORT}]}],
        },
    }


def _service_exposure(spec: dict, labels: dict) -> list[str]:
    found = [] if spec.get("type") == "ClusterIP" else [f"service type {spec.get('type')} is not ClusterIP"]
    found += [f"service sets {name}" for name in PUBLIC_SERVICE_FIELDS if name in spec]
    for port in spec.get("ports", []):
        if "nodePort" in port or port.get("port") != PORT or port.get("targetPort") != PORT:
            found.append(f"service port {port.get('port')} is not the private terminal port")
    if spec.get("selector") != labels:
        found.append("service selector is not this execution's identity")
    return found


def _policy_exposure(spec: dict, labels: dict) -> list[str]:
    found = [] if spec.get("podSelector") == {"matchLabels": labels} else ["policy selects another Pod"]
    for rule in spec.get("ingress", []):
        peers = rule.get("from")
        if not peers or any("ipBlock" in peer or not peer.get("podSelector", {}).get("matchLabels") for peer in peers):
            found.append("policy admits clients beyond the selected terminal clients")
    return found


def public_exposure(endpoint: Endpoint, manifest: dict) -> list[str]:
    checks = {"Service": _service_exposure, "NetworkPolicy": _policy_exposure}
    check = checks.get(manifest.get("kind"))
    return check(manifest.get("spec", {}), endpoint.labels) if check else []


def objects(endpoint: Endpoint, pod: dict, identity: HostIdentity, user_ca: str, clients: Clients) -> list[dict]:
    owner = _owned(endpoint, pod)
    rendered = [
        host_secret(endpoint, owner, identity, user_ca),
        service(endpoint, owner),
        network_policy(endpoint, owner, clients),
    ]
    for manifest in rendered:
        if found := public_exposure(endpoint, manifest):
            raise EndpointRefused("public_exposure", "; ".join(found))
    return rendered


def with_terminal(pod: dict, endpoint: Endpoint) -> dict:
    mounted = copy.deepcopy(pod)
    spec = mounted["spec"]
    spec["volumes"].append({"name": VOLUME, "secret": {"secretName": endpoint.name, "defaultMode": 0o440}})
    container = spec["containers"][0]
    container["volumeMounts"].append({"name": VOLUME, "mountPath": MATERIAL_DIR, "readOnly": True})
    container["ports"] = [{"name": PORT_NAME, "containerPort": PORT, "protocol": "TCP"}]
    return mounted
