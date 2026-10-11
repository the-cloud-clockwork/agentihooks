"""Compare a live Pod with the Pod rendered for its operation, tolerating the fields the API server and admission add."""

HOST_NAMESPACES = ("hostNetwork", "hostPID", "hostIPC")


def _containers(spec: dict) -> list[dict]:
    return [*spec.get("initContainers", []), *spec.get("containers", [])]


def _images(spec: dict) -> dict:
    return {container.get("name"): container.get("image") for container in _containers(spec)}


def _arguments(spec: dict) -> dict:
    return {container.get("name"): (container.get("command"), container.get("args")) for container in _containers(spec)}


def _secret_refs(spec: dict) -> list[tuple]:
    refs = []
    for container in _containers(spec):
        for env in container.get("env", []):
            if ref := env.get("valueFrom", {}).get("secretKeyRef"):
                refs.append(("env", container.get("name"), env.get("name"), ref.get("name"), ref.get("key")))
        for source in container.get("envFrom", []):
            if "secretRef" in source:
                refs.append(("envFrom", container.get("name"), source["secretRef"].get("name")))
    for volume in spec.get("volumes", []):
        if "secret" in volume:
            refs.append(("volume", volume.get("name"), volume["secret"].get("secretName")))
        for source in volume.get("projected", {}).get("sources", []):
            if "secret" in source:
                refs.append(("projected", volume.get("name"), source["secret"].get("name")))
    return sorted(refs, key=repr)


def _pull_secrets(expected: dict, live: dict) -> bool:
    # ServiceAccount admission adds the account's pull secrets only to a Pod that names none.
    return "imagePullSecrets" not in expected or live.get("imagePullSecrets") == expected["imagePullSecrets"]


def _account(spec: dict) -> tuple:
    return spec.get("serviceAccountName"), spec.get("automountServiceAccountToken")


def _covers(expected: dict, live: object) -> bool:
    return isinstance(live, dict) and all(live.get(key) == value for key, value in expected.items())


def _secured(expected: dict, live: dict) -> bool:
    if not _covers(expected.get("securityContext", {}), live.get("securityContext", {})):
        return False
    if any(bool(live.get(key, False)) != bool(expected.get(key, False)) for key in HOST_NAMESPACES):
        return False
    running = {container.get("name"): container for container in _containers(live)}
    return all(
        _covers(container.get("securityContext", {}), running.get(container.get("name"), {}).get("securityContext"))
        for container in _containers(expected)
    )


def mismatch(rendered: dict, live: dict) -> str:
    expected, actual = rendered.get("spec", {}), live.get("spec", {})
    if _images(expected) != _images(actual):
        return "image"
    if _arguments(expected) != _arguments(actual):
        return "arguments"
    if _secret_refs(expected) != _secret_refs(actual) or not _pull_secrets(expected, actual):
        return "secrets"
    if _account(expected) != _account(actual):
        return "service-account"
    if not _secured(expected, actual):
        return "security-context"
    return ""
