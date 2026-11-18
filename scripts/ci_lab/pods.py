"""Pod deletion cost on lab workers: the scheduler marks a worker's pod busy while the worker holds a lease, so a
ReplicaSet scale down removes idle workers first."""

import http.client
import json
import ssl
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote_plus

from scripts.swarm_v2.kubernetes.client import ACCOUNT, KubeHttp

__all__ = [
    "ANNOTATION",
    "BUSY",
    "MERGE_PATCH",
    "SERVICE_HOST",
    "TIMEOUT_S",
    "KubePods",
    "Pods",
    "Unmanaged",
    "worker_pods",
]

ANNOTATION = "controller.kubernetes.io/pod-deletion-cost"
BUSY = "1000"
MERGE_PATCH = "application/merge-patch+json"
SERVICE_HOST = "KUBERNETES_SERVICE_HOST"
TIMEOUT_S = 5
Opener = Callable[..., Any]


class Pods(Protocol):
    def cost(self, pod: str, busy: bool) -> bool:
        """Set the busy deletion cost on the pod, or clear it; whether the API server took the patch."""
        ...


class Unmanaged:
    """The pods port outside a cluster, where no worker runs as a pod: it patches nothing."""

    def cost(self, pod: str, busy: bool) -> bool:
        """Patch nothing and answer False."""
        return False


@dataclass(frozen=True)
class KubePods:
    """Merge patches on the lab namespace's pods over the API server, with the service account token."""

    server: str
    namespace: str
    token: Path
    context: ssl.SSLContext
    opener: Opener = urllib.request.urlopen

    def cost(self, pod: str, busy: bool) -> bool:
        """Patch the pod's deletion cost annotation to the busy cost, or to null to clear it; False when the API
        server refused the patch or could not be reached."""
        body = {"metadata": {"annotations": {ANNOTATION: BUSY if busy else None}}}
        url = f"{self.server}/api/v1/namespaces/{self.namespace}/pods/{quote_plus(pod)}"
        request = urllib.request.Request(url, data=json.dumps(body).encode(), method="PATCH")
        request.add_header("Content-Type", MERGE_PATCH)
        try:
            request.add_header("Authorization", f"Bearer {self.token.read_text().strip()}")
            with self.opener(request, timeout=TIMEOUT_S, context=self.context):
                return True
        except (OSError, http.client.HTTPException):
            return False


def worker_pods(environ: Mapping[str, str], account: Path = ACCOUNT) -> Pods:
    """Return the pods client of the service account's namespace inside a cluster, else Unmanaged."""
    if SERVICE_HOST not in environ:
        return Unmanaged()
    api = KubeHttp.in_cluster(environ, account)
    namespace = (account / "namespace").read_text().strip()
    return KubePods(api.server, namespace, api.token_path, api.context)
