import os
from collections.abc import Callable, Mapping

ENV = {"namespace": "AGENTIHOOKS_POD_NAMESPACE", "name": "AGENTIHOOKS_POD_NAME", "uid": "AGENTIHOOKS_POD_UID"}
ENDED = ("Failed", "Succeeded")


def identity(environ: Mapping[str, str]) -> dict:
    pod = {field: environ.get(name, "") for field, name in ENV.items()}
    return pod if all(pod.values()) else {}


def _complete(pod: object) -> bool:
    return isinstance(pod, dict) and all(isinstance(pod.get(field), str) and pod[field] for field in ENV)


def read_pod(namespace: str, name: str) -> dict | None:
    from scripts.swarm_v2.kubernetes.client import KubeHttp, PodClient

    return PodClient(KubeHttp.in_cluster(os.environ), namespace).read_pod(name)


def gone(held: object, own: dict, read: Callable[[str, str], dict | None] = read_pod) -> bool:
    from scripts.swarm_v2.kubernetes.client import ApiRefused

    if not own or not _complete(held):
        return False
    if held["uid"] == own["uid"]:
        return True
    try:
        pod = read(held["namespace"], held["name"])
    except (ApiRefused, KeyError, OSError):
        return False
    if pod is None:
        return True
    return pod.get("metadata", {}).get("uid") != held["uid"] or pod.get("status", {}).get("phase") in ENDED
