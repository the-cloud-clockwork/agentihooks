"""The security relevant fields of a Pod spec, compared exactly; admission may default every other field."""

import re

from scripts.swarm_v2.kubernetes.spec import canonical_digest

GROUPS = (
    "image",
    "arguments",
    "secrets",
    "environment",
    "service-account",
    "volumes",
    "network",
    "security-context",
)
HOST_SHARING = ("hostNetwork", "hostPID", "hostIPC", "shareProcessNamespace")
KINDS = ("initContainers", "containers", "ephemeralContainers")
PROBES = ("startupProbe", "readinessProbe", "livenessProbe")
HANDLERS = ("exec", "httpGet", "tcpSocket", "grpc")
QUANTITY = re.compile(r"(\d+)([KMGTPE]i|[kMGTPE]?)")
UNITS = (
    {"": 1}
    | {f"{p}i": 1024 ** (i + 1) for i, p in enumerate("KMGTPE")}
    | {p: 1000 ** (i + 1) for i, p in enumerate("kMGTPE")}
)


def _containers(spec: dict) -> list[dict]:
    return [container for kind in KINDS for container in spec.get(kind, [])]


def _named(spec: dict, *keys: str) -> dict:
    return {container.get("name", ""): [container.get(key) for key in keys] for container in _containers(spec)}


def _secret_refs(spec: dict) -> list[list]:
    refs = [["pull", ref.get("name")] for ref in spec.get("imagePullSecrets", [])]
    for container in _containers(spec):
        for env in container.get("env", []):
            if ref := env.get("valueFrom", {}).get("secretKeyRef"):
                refs.append(["env", container.get("name", ""), env.get("name"), ref.get("name"), ref.get("key")])
        for source in container.get("envFrom", []):
            if "secretRef" in source:
                refs.append(["envFrom", container.get("name", ""), source["secretRef"].get("name")])
    for volume in spec.get("volumes", []):
        if "secret" in volume:
            refs.append(["volume", volume.get("name"), volume["secret"].get("secretName")])
        for source in volume.get("projected", {}).get("sources", []):
            if "secret" in source:
                refs.append(["projected", volume.get("name"), source["secret"].get("name")])
    return sorted(refs, key=repr)


def _commands(spec: dict) -> dict:
    return {
        container.get("name", ""): [
            *(container.get(key) for key in ("command", "args", "workingDir", "lifecycle")),
            {probe: {key: (container.get(probe) or {}).get(key) for key in HANDLERS} for probe in PROBES},
        ]
        for container in _containers(spec)
    }


def _bytes(quantity: object) -> object:
    match = QUANTITY.fullmatch(str(quantity))
    return int(match[1]) * UNITS[match[2]] if match else quantity


def _volume(volume: dict) -> dict:
    # The API server rewrites an emptyDir size into its canonical quantity.
    if "sizeLimit" not in volume.get("emptyDir", {}):
        return volume
    return {**volume, "emptyDir": {**volume["emptyDir"], "sizeLimit": _bytes(volume["emptyDir"]["sizeLimit"])}}


def _host_ports(spec: dict) -> dict:
    return {
        container.get("name", ""): [[port.get("hostPort"), port.get("hostIP")] for port in container.get("ports", [])]
        for container in _containers(spec)
    }


def fields(spec: dict) -> dict:
    return {
        "image": _named(spec, "image"),
        "arguments": _commands(spec),
        "secrets": _secret_refs(spec),
        "environment": _named(spec, "env", "envFrom"),
        "service-account": [spec.get("serviceAccountName"), spec.get("automountServiceAccountToken")],
        "volumes": [[_volume(volume) for volume in spec.get("volumes", [])], _named(spec, "volumeMounts")],
        "network": [spec.get("hostAliases"), spec.get("dnsConfig"), _host_ports(spec)],
        "security-context": {
            "pod": spec.get("securityContext"),
            "containers": _named(spec, "securityContext"),
            "host": [bool(spec.get(key)) for key in HOST_SHARING],
            "hostUsers": spec.get("hostUsers"),
            "runtimeClassName": spec.get("runtimeClassName"),
        },
    }


def identity(pod_digest: object, spec: dict) -> str:
    return canonical_digest({"pod": pod_digest, "fields": fields(spec)})


def mismatch(rendered: dict, live: dict) -> str:
    expected, actual = fields(rendered.get("spec", {})), fields(live.get("spec", {}))
    return next((group for group in GROUPS if expected[group] != actual[group]), "")
