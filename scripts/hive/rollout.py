import argparse
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

Json = dict[str, Any]
LEASE_SECONDS = 3600
ACQUIRE_SECONDS = LEASE_SECONDS + 1800


class Conflict(Exception):
    pass


class Kubernetes:
    def __init__(self):
        from scripts.swarm_v2.kubernetes.client import KubeHttp

        self.api = KubeHttp.in_cluster(os.environ)

    def request(self, method: str, path: str, body: Json | None = None) -> Json | None:
        status, result = self.api.send(method, path, body)
        if status == 409:
            raise Conflict()
        if status == 404 and method == "GET":
            return None
        if status not in (200, 201):
            raise RuntimeError(f"Kubernetes refused rollout request: {status}")
        return result


def serving(deployment: Json) -> bool:
    replicas = deployment["spec"].get("replicas", 1)
    status = deployment.get("status", {})
    return (
        replicas > 0
        and status.get("observedGeneration", 0) >= deployment["metadata"]["generation"]
        and all(status.get(key, 0) == replicas for key in ("updatedReplicas", "readyReplicas", "availableReplicas"))
    )


def lease_path(namespace: str, name: str) -> str:
    return f"/apis/coordination.k8s.io/v1/namespaces/{quote(namespace, safe='')}/leases/{quote(name, safe='')}"


def micro_time(seconds: float) -> str:
    """Render epoch seconds as a Kubernetes MicroTime string."""
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def expired(spec: Json, now: float) -> bool:
    """Tell whether a held lease has passed its renew time plus its duration."""
    renewed, duration = spec.get("renewTime"), spec.get("leaseDurationSeconds")
    if not renewed or not duration:
        return False
    return now > datetime.strptime(renewed, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC).timestamp() + duration


def acquire(api: Kubernetes, namespace: str, name: str, owner: str, timeout: int = ACQUIRE_SECONDS) -> None:
    """Take the rollout lease for owner, waiting for a free or expired lease until timeout."""
    path = lease_path(namespace, name)
    deadline = time.monotonic() + timeout
    while True:
        lease = api.request("GET", path)
        if lease is not None and lease["spec"].get("holderIdentity") == owner:
            return
        now = time.time()
        if lease is None or not lease["spec"].get("holderIdentity") or expired(lease["spec"], now):
            wanted = lease or {
                "apiVersion": "coordination.k8s.io/v1",
                "kind": "Lease",
                "metadata": {"name": name, "namespace": namespace},
                "spec": {},
            }
            wanted["spec"].update(holderIdentity=owner, leaseDurationSeconds=LEASE_SECONDS, renewTime=micro_time(now))
            try:
                api.request("PUT" if lease else "POST", path if lease else path.rsplit("/", 1)[0], wanted)
                return
            except Conflict:
                pass
        if time.monotonic() >= deadline:
            raise TimeoutError("another hive rollout owns the lease; no workload was changed")
        time.sleep(5)


def abandon(api: Kubernetes, namespace: str, name: str, owner: str) -> None:
    """Free the rollout lease after a failed sync when owner still holds it."""
    path = lease_path(namespace, name)
    lease = api.request("GET", path)
    if lease is None or lease["spec"].get("holderIdentity") != owner:
        return
    lease["spec"]["holderIdentity"] = ""
    api.request("PUT", path, lease)


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
    parser.add_argument("action", choices=["acquire", "release", "abandon", "ready"])
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
    elif args.action == "abandon":
        abandon(api, args.namespace, args.name, args.owner)
    else:
        release(api, args.namespace, args.name, args.owner, args.hive_namespace, args.deployment)


if __name__ == "__main__":
    main()
