import argparse
import os
import time
from pathlib import Path
from urllib.parse import quote


class Conflict(Exception):
    pass


class Kubernetes:
    def __init__(self):
        from scripts.swarm_v2.kubernetes.client import KubeHttp

        self.api = KubeHttp.in_cluster(os.environ)

    def request(self, method: str, path: str, body: dict | None = None) -> dict | None:
        status, result = self.api.send(method, path, body)
        if status == 409:
            raise Conflict()
        if status == 404 and method == "GET":
            return None
        if status not in (200, 201):
            raise RuntimeError(f"Kubernetes refused rollout request: {status}")
        return result


def serving(deployment: dict) -> bool:
    replicas = deployment["spec"].get("replicas", 1)
    status = deployment.get("status", {})
    return (
        replicas > 0
        and status.get("observedGeneration", 0) >= deployment["metadata"]["generation"]
        and all(status.get(key, 0) == replicas for key in ("updatedReplicas", "readyReplicas", "availableReplicas"))
    )


def lease_path(namespace: str, name: str) -> str:
    return f"/apis/coordination.k8s.io/v1/namespaces/{quote(namespace, safe='')}/leases/{quote(name, safe='')}"


def acquire(api: Kubernetes, namespace: str, name: str, owner: str, timeout: int = 1800) -> None:
    path = lease_path(namespace, name)
    deadline = time.monotonic() + timeout
    while True:
        lease = api.request("GET", path)
        if lease is not None and lease["spec"].get("holderIdentity") == owner:
            return
        if lease is None or not lease["spec"].get("holderIdentity"):
            wanted = lease or {
                "apiVersion": "coordination.k8s.io/v1",
                "kind": "Lease",
                "metadata": {"name": name, "namespace": namespace},
                "spec": {},
            }
            wanted["spec"]["holderIdentity"] = owner
            try:
                api.request("PUT" if lease else "POST", path if lease else path.rsplit("/", 1)[0], wanted)
                return
            except Conflict:
                pass
        if time.monotonic() >= deadline:
            raise TimeoutError("another hive rollout owns the lease; no workload was changed")
        time.sleep(5)


def release(api: Kubernetes, namespace: str, name: str, owner: str, hive_namespace: str, deployment: str) -> None:
    path = lease_path(namespace, name)
    lease = api.request("GET", path)
    if lease is None or lease["spec"].get("holderIdentity") != owner:
        raise RuntimeError("only the rollout owner can release the lease")
    for worker in deployment.split(","):
        workload = api.request(
            "GET", f"/apis/apps/v1/namespaces/{quote(hive_namespace, safe='')}/deployments/{quote(worker, safe='')}"
        )
        if workload is None or not serving(workload):
            raise RuntimeError("replacement hive is not serving; rollout ownership retained")
    lease["spec"]["holderIdentity"] = ""
    api.request("PUT", path, lease)


def ready(marker: Path, now: float) -> bool:
    return marker.is_file() and 0 <= now - marker.stat().st_mtime < 60


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["acquire", "release", "ready"])
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--name", default="agentihooks-hive-rollout")
    parser.add_argument("--owner", default="")
    parser.add_argument("--hive-namespace", default="")
    parser.add_argument("--deployment", default="")
    args = parser.parse_args()
    if args.action == "ready":
        raise SystemExit(0 if ready(Path(os.environ["HIVE_READY_FILE"]), time.time()) else 1)
    api = Kubernetes()
    if args.action == "acquire":
        acquire(api, args.namespace, args.name, args.owner)
    else:
        release(api, args.namespace, args.name, args.owner, args.hive_namespace, args.deployment)


if __name__ == "__main__":
    main()
