"""Pod deletion cost on lab workers: the scheduler marks a worker's pod busy while the worker holds a lease, so a
ReplicaSet scale down removes idle workers first."""

import http.client
import json
import ssl
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from scripts.swarm_v2.kubernetes.client import ACCOUNT, KubeHttp

__all__ = [
    "ANNOTATION",
    "AUTHORIZATION",
    "BUSY",
    "CONTENT_TYPE",
    "MERGE_PATCH",
    "SERVICE_HOST",
    "TIMEOUT_S",
    "Images",
    "KubePods",
    "PodImages",
    "Pods",
    "pod_images",
    "unmanaged",
    "unread",
    "worker_pods",
]

ANNOTATION = "controller.kubernetes.io/pod-deletion-cost"
BUSY = "1000"
MERGE_PATCH = "application/merge-patch+json"
CONTENT_TYPE = "Content-Type"
AUTHORIZATION = "Authorization"
SERVICE_HOST = "KUBERNETES_SERVICE_HOST"
TIMEOUT_S = 5
Opener = Callable[..., Any]
Pods = Callable[[str, bool], bool]


def unmanaged(pod: str, busy: bool) -> bool:
    """The pods port outside a cluster, where no worker runs as a pod: patch nothing and answer False."""
    return False


@dataclass(frozen=True)
class Images:
    """A pod's declared spec image and the digest its container actually runs."""

    declared: str
    running: str


PodImages = Callable[[str], Images | None]


def unread(pod: str) -> Images | None:
    """The pod images port outside a cluster, where no worker runs as a pod: read nothing."""
    return None


@dataclass(frozen=True)
class KubePods:
    """Merge patches on the lab namespace's pods over the API server, with the service account token."""

    server: str
    namespace: str
    token: Path
    context: ssl.SSLContext
    opener: Opener = urllib.request.urlopen

    def __call__(self, pod: str, busy: bool) -> bool:
        """Patch the pod's deletion cost annotation to the busy cost, or to null to clear it; False when the API
        server refused the patch or could not be reached."""
        body = {"metadata": {"annotations": {ANNOTATION: BUSY if busy else None}}}
        url = f"{self.server}/api/v1/namespaces/{self.namespace}/pods/{quote_plus(pod)}"
        request = urllib.request.Request(url, data=json.dumps(body).encode(), method="PATCH")
        request.add_header(CONTENT_TYPE, MERGE_PATCH)
        try:
            request.add_header(AUTHORIZATION, f"Bearer {self.token.read_text().strip()}")
            with self.opener(request, timeout=TIMEOUT_S, context=self.context):
                return True
        except (OSError, http.client.HTTPException):
            return False

    def images(self, pod: str) -> Images | None:
        """Read the pod's declared and running image; None when the pod has no container status yet, the running
        image carries no digest, or the API server could not be reached."""
        return None


def pod_images(pods: Pods) -> PodImages:
    """Return the pod image reader bound to a KubePods port, else the unmanaged reader."""
    return pods.images if isinstance(pods, KubePods) else unread


def worker_pods(environ: Mapping[str, str], account: Path = ACCOUNT) -> Pods:
    """Return the pods client of the service account's namespace inside a cluster, else unmanaged."""
    if SERVICE_HOST not in environ:
        return unmanaged
    api = KubeHttp.in_cluster(environ, account)
    namespace = (account / "namespace").read_text().strip()
    return KubePods(api.server, namespace, api.token_path, api.context)
