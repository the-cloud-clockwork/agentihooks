import http.client
import json
import ssl
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from scripts.ci_lab import pods
from scripts.swarm_v2.kubernetes import client

CONTEXT = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
SERVER = "https://api:6443"


class Response:
    def __init__(self, body: bytes = b"{}") -> None:
        self.body = body

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *raised: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class Opener:
    def __init__(self, error: Exception | None = None, body: bytes = b"{}") -> None:
        self.error = error
        self.body = body
        self.calls: list[tuple[urllib.request.Request, dict[str, Any]]] = []

    def __call__(self, request: urllib.request.Request, **options: Any) -> Response:
        self.calls.append((request, options))
        if self.error is not None:
            raise self.error
        return Response(self.body)


def kube(tmp_path: Path, opener: Opener) -> pods.KubePods:
    token = tmp_path / "token"
    token.write_text(" tok\n")
    return pods.KubePods(SERVER, "ci-lab", token, CONTEXT, opener)


@pytest.mark.feature("lab-worker-deletion-cost")
@pytest.mark.parametrize(("busy", "cost"), [(True, "1000"), (False, None)])
def test_a_cost_is_one_merge_patch_on_the_pod_with_the_service_account_token(
    tmp_path: Path, busy: bool, cost: str | None
) -> None:
    opener = Opener()
    assert kube(tmp_path, opener)("ci-lab-worker-7d9f-abc", busy) is True
    [(request, options)] = opener.calls
    assert request.get_method() == "PATCH"
    assert request.full_url == f"{SERVER}/api/v1/namespaces/ci-lab/pods/ci-lab-worker-7d9f-abc"
    assert request.get_header("Content-type") == "application/merge-patch+json"
    assert request.get_header("Authorization") == "Bearer tok"
    assert isinstance(request.data, bytes)
    annotations = {"controller.kubernetes.io/pod-deletion-cost": cost}
    assert json.loads(request.data) == {"metadata": {"annotations": annotations}}
    assert options == {"timeout": 5, "context": CONTEXT}


@pytest.mark.feature("lab-worker-deletion-cost")
def test_a_worker_name_stays_one_path_segment_of_the_pod_url(tmp_path: Path) -> None:
    opener = Opener()
    kube(tmp_path, opener)("../secrets/x", True)
    assert opener.calls[0][0].full_url == f"{SERVER}/api/v1/namespaces/ci-lab/pods/..%2Fsecrets%2Fx"


@pytest.mark.feature("lab-worker-deletion-cost")
@pytest.mark.parametrize(
    "error",
    [
        urllib.error.HTTPError(SERVER, 403, "Forbidden", Message(), None),
        urllib.error.URLError("refused"),
        TimeoutError("slow"),
        http.client.RemoteDisconnected("gone"),
        http.client.BadStatusLine("garbled"),
    ],
)
def test_a_refused_or_unreachable_api_server_answers_false(tmp_path: Path, error: Exception) -> None:
    opener = Opener(error)
    assert kube(tmp_path, opener)("ci-lab-worker-1", True) is False
    assert len(opener.calls) == 1


@pytest.mark.feature("lab-worker-deletion-cost")
def test_an_unreadable_token_answers_false_and_sends_nothing(tmp_path: Path) -> None:
    opener = Opener()
    api = pods.KubePods(SERVER, "ci-lab", tmp_path / "missing", CONTEXT, opener)
    assert api("ci-lab-worker-1", True) is False
    assert opener.calls == []


@pytest.mark.feature("lab-worker-deletion-cost")
def test_outside_a_cluster_the_pods_port_patches_nothing() -> None:
    assert pods.worker_pods({"KUBERNETES_SERVICE_PORT": "443"}) is pods.unmanaged
    assert pods.unmanaged("ci-lab-worker-1", True) is False
    assert pods.unmanaged("ci-lab-worker-1", False) is False


@pytest.mark.feature("lab-worker-deletion-cost")
def test_in_a_cluster_the_pods_port_patches_in_the_service_account_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authorities: list[str] = []

    def trusted(cafile: str) -> ssl.SSLContext:
        authorities.append(cafile)
        return CONTEXT

    monkeypatch.setattr(client.ssl, "create_default_context", trusted)
    (tmp_path / "namespace").write_text("ci-lab\n")
    environ = {"KUBERNETES_SERVICE_HOST": "10.43.0.1", "KUBERNETES_SERVICE_PORT": "443"}
    built = pods.worker_pods(environ, tmp_path)
    assert built == pods.KubePods("https://10.43.0.1:443", "ci-lab", tmp_path / "token", CONTEXT)
    assert authorities == [str(tmp_path / "ca.crt")]


def pod_body(image: str, image_id: str) -> bytes:
    return json.dumps(
        {"spec": {"containers": [{"image": image}]}, "status": {"containerStatuses": [{"imageID": image_id}]}}
    ).encode()


@pytest.mark.feature("lab-worker-seat-keeping-restart")
def test_a_floating_pods_image_read_is_one_get_with_the_service_account_token(tmp_path: Path) -> None:
    opener = Opener(body=pod_body("repo/lab-worker:dev", "repo/lab-worker@sha256:abc"))
    assert kube(tmp_path, opener).images("ci-lab-worker-7d9f-abc") == pods.Images(
        "repo/lab-worker:dev", "repo/lab-worker@sha256:abc"
    )
    [(request, options)] = opener.calls
    assert request.get_method() == "GET"
    assert request.full_url == f"{SERVER}/api/v1/namespaces/ci-lab/pods/ci-lab-worker-7d9f-abc"
    assert request.get_header("Authorization") == "Bearer tok"
    assert options == {"timeout": 5, "context": CONTEXT}


@pytest.mark.feature("lab-worker-seat-keeping-restart")
def test_a_pinned_pods_image_read_carries_its_declared_digest_too(tmp_path: Path) -> None:
    opener = Opener(body=pod_body("repo/lab-worker@sha256:abc", "repo/lab-worker@sha256:abc"))
    assert kube(tmp_path, opener).images("ci-lab-worker-1") == pods.Images(
        "repo/lab-worker@sha256:abc", "repo/lab-worker@sha256:abc"
    )


@pytest.mark.feature("lab-worker-seat-keeping-restart")
def test_a_pod_with_no_container_status_yet_reads_as_unknown(tmp_path: Path) -> None:
    body = json.dumps({"spec": {"containers": [{"image": "repo/lab-worker:dev"}]}, "status": {}}).encode()
    opener = Opener(body=body)
    assert kube(tmp_path, opener).images("ci-lab-worker-1") is None


@pytest.mark.feature("lab-worker-seat-keeping-restart")
def test_a_running_image_id_with_no_digest_reads_as_unknown(tmp_path: Path) -> None:
    opener = Opener(body=pod_body("repo/lab-worker:dev", "repo/lab-worker:dev"))
    assert kube(tmp_path, opener).images("ci-lab-worker-1") is None


@pytest.mark.feature("lab-worker-seat-keeping-restart")
@pytest.mark.parametrize(
    ("image", "image_id"),
    [
        (None, "repo/lab-worker@sha256:abc"),
        (7, "repo/lab-worker@sha256:abc"),
        ("repo/lab-worker:dev", None),
        ("repo/lab-worker:dev", 7),
    ],
)
def test_an_image_or_image_id_that_is_no_string_reads_as_unknown(
    tmp_path: Path, image: object, image_id: object
) -> None:
    body = json.dumps(
        {"spec": {"containers": [{"image": image}]}, "status": {"containerStatuses": [{"imageID": image_id}]}}
    ).encode()
    assert kube(tmp_path, Opener(body=body)).images("ci-lab-worker-1") is None


@pytest.mark.feature("lab-worker-seat-keeping-restart")
@pytest.mark.parametrize(
    "error",
    [
        urllib.error.HTTPError(SERVER, 403, "Forbidden", Message(), None),
        urllib.error.URLError("refused"),
        TimeoutError("slow"),
        http.client.RemoteDisconnected("gone"),
        http.client.BadStatusLine("garbled"),
    ],
)
def test_a_refused_or_unreachable_api_server_reads_the_image_as_unknown(tmp_path: Path, error: Exception) -> None:
    assert kube(tmp_path, Opener(error)).images("ci-lab-worker-1") is None


@pytest.mark.feature("lab-worker-seat-keeping-restart")
def test_an_unreadable_token_reads_the_image_as_unknown_and_sends_nothing(tmp_path: Path) -> None:
    opener = Opener()
    api = pods.KubePods(SERVER, "ci-lab", tmp_path / "missing", CONTEXT, opener)
    assert api.images("ci-lab-worker-1") is None
    assert opener.calls == []


@pytest.mark.feature("lab-worker-seat-keeping-restart")
def test_pod_images_gives_the_kube_reader_for_a_kube_pods_port_and_unread_otherwise(tmp_path: Path) -> None:
    api = kube(tmp_path, Opener())
    assert pods.pod_images(api) == api.images
    assert pods.pod_images(pods.unmanaged) is pods.unread


@pytest.mark.feature("lab-worker-seat-keeping-restart")
def test_unread_answers_none() -> None:
    assert pods.unread("ci-lab-worker-1") is None
