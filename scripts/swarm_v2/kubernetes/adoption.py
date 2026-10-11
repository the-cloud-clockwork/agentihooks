"""The security relevant fields of a Pod spec, compared exactly; admission may default every other field."""

from scripts.swarm_v2.kubernetes.spec import canonical_digest

GROUPS = ("image", "arguments", "secrets", "environment", "service-account", "volumes", "security-context")
HOST_SHARING = ("hostNetwork", "hostPID", "hostIPC", "shareProcessNamespace")
KINDS = ("initContainers", "containers", "ephemeralContainers")


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


def _volume(volume: dict) -> dict:
    # The API server rewrites an emptyDir size into its canonical quantity.
    if "emptyDir" not in volume:
        return volume
    return {**volume, "emptyDir": {key: value for key, value in volume["emptyDir"].items() if key != "sizeLimit"}}


def fields(spec: dict) -> dict:
    return {
        "image": _named(spec, "image"),
        "arguments": _named(spec, "command", "args"),
        "secrets": _secret_refs(spec),
        "environment": _named(spec, "env", "envFrom"),
        "service-account": [spec.get("serviceAccountName"), spec.get("automountServiceAccountToken")],
        "volumes": [[_volume(volume) for volume in spec.get("volumes", [])], _named(spec, "volumeMounts")],
        "security-context": {
            "pod": spec.get("securityContext"),
            "containers": _named(spec, "securityContext"),
            "host": [bool(spec.get(key)) for key in HOST_SHARING],
            "runtimeClassName": spec.get("runtimeClassName"),
        },
    }


def identity(pod_digest: object, spec: dict) -> str:
    return canonical_digest({"pod": pod_digest, "fields": fields(spec)})


def mismatch(rendered: dict, live: dict) -> str:
    expected, actual = fields(rendered.get("spec", {})), fields(live.get("spec", {}))
    return next((group for group in GROUPS if expected[group] != actual[group]), "")
