import argparse
import json
import os
import ssl
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


class Conflict(Exception):
    pass


class Kubernetes:
    def __init__(self):
        self.root = f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.environ['KUBERNETES_SERVICE_PORT']}"
        self.credentials = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        self.context = ssl.create_default_context(cafile=str(self.credentials / "ca.crt"))

    def request(self, method: str, path: str, body: dict | None = None) -> dict | None:
        request = Request(
            self.root + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {(self.credentials / 'token').read_text().strip()}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urlopen(request, context=self.context, timeout=10) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code == 409:
                raise Conflict() from error
            if error.code == 404 and method == "GET":
                return None
            raise


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
    workload = api.request(
        "GET", f"/apis/apps/v1/namespaces/{quote(hive_namespace, safe='')}/deployments/{quote(deployment, safe='')}"
    )
    if workload is None or not serving(workload):
        raise RuntimeError("replacement hive is not serving; rollout ownership retained")
    lease["spec"]["holderIdentity"] = ""
    api.request("PUT", path, lease)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["acquire", "release"])
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--hive-namespace", default="")
    parser.add_argument("--deployment", default="")
    args = parser.parse_args()
    api = Kubernetes()
    if args.action == "acquire":
        acquire(api, args.namespace, args.name, args.owner)
    else:
        release(api, args.namespace, args.name, args.owner, args.hive_namespace, args.deployment)


if __name__ == "__main__":
    main()
