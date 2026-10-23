import os
import re
from collections.abc import Callable, Mapping

ENV = {"namespace": "AGENTIHOOKS_POD_NAMESPACE", "name": "AGENTIHOOKS_POD_NAME", "uid": "AGENTIHOOKS_POD_UID"}
CONTAINER_ENV = "AGENTIHOOKS_POD_CONTAINER"
ENDED = ("Failed", "Succeeded")
DNS_NAMES = {
    "namespace": re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?"),
    "name": re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?"),
}


def pod(environ: Mapping[str, str]) -> dict:
    found = {field: environ.get(name, "") for field, name in ENV.items()}
    return found if all(found.values()) else {}


def read_pod(namespace: str, name: str) -> dict | None:
    from scripts.swarm_v2.kubernetes.client import KubeHttp, PodClient

    return PodClient(KubeHttp.in_cluster(os.environ), namespace).read_pod(name)


def _read(read: Callable[[str, str], dict | None], namespace: str, name: str) -> tuple[bool, dict | None]:
    from scripts.swarm_v2.kubernetes.client import ApiRefused

    try:
        return True, read(namespace, name)
    except (ApiRefused, KeyError, OSError):
        return False, None


def _statuses(found: dict) -> list:
    return found.get("status", {}).get("containerStatuses", [])


def identity(environ: Mapping[str, str], read: Callable[[str, str], dict | None] = read_pod) -> dict:
    own, container = pod(environ), environ.get(CONTAINER_ENV, "")
    if not own or not container:
        return own
    _, found = _read(read, own["namespace"], own["name"])
    ids = [s.get("containerID", "") for s in _statuses(found or {}) if s.get("name") == container]
    return {**own, "container": ids[0]} if ids and ids[0] else own


def _complete(held: object) -> bool:
    return (
        isinstance(held, dict)
        and all(isinstance(held.get(field), str) and held[field] for field in ENV)
        and all(pattern.fullmatch(held[field]) for field, pattern in DNS_NAMES.items())
    )


def _running(found: dict, container: str) -> bool:
    return any(s.get("containerID") == container and "running" in s.get("state", {}) for s in _statuses(found))


def gone(held: object, own: dict, read: Callable[[str, str], dict | None] = read_pod) -> bool:
    if not own or not _complete(held):
        return False
    answered, found = _read(read, held["namespace"], held["name"])
    if not answered:
        return False
    if found is None:
        return True
    if found.get("metadata", {}).get("uid") != held["uid"] or found.get("status", {}).get("phase") in ENDED:
        return True
    container = held.get("container")
    return isinstance(container, str) and bool(container) and not _running(found, container)
