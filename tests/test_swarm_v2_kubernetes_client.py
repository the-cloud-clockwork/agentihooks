import io
import json
import ssl
import urllib.error
from http.client import IncompleteRead

import pytest

from scripts.swarm_v2.kubernetes import client
from scripts.swarm_v2.kubernetes.client import AlreadyExists, ApiRefused, KubeHttp, PodClient

pytestmark = pytest.mark.unit

CONTEXT = object()


class Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status, self.body = status, body

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


class Opener:
    def __init__(self, *answers) -> None:
        self.answers, self.calls = list(answers), []

    def __call__(self, request, timeout, context):
        self.calls.append((request, timeout, context))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return Response(status, json.dumps(body).encode())


def http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.test/x", code, "refused", {}, io.BytesIO(body))


@pytest.fixture
def token(tmp_path):
    path = tmp_path / "token"
    path.write_text("first-token\n")
    return path


def http(token, *answers) -> tuple[KubeHttp, Opener]:
    opener = Opener(*answers)
    return KubeHttp("https://api.test:6443", token, CONTEXT, opener), opener


def test_send_posts_json_with_a_bearer_token_and_the_bounded_timeout(token):
    api, opener = http(token, (201, {"kind": "Pod"}))
    assert api.send("POST", "/api/v1/pods", {"a": "é"}) == (201, {"kind": "Pod"})
    request, timeout, context = opener.calls[0]
    assert request.full_url == "https://api.test:6443/api/v1/pods"
    assert request.get_method() == "POST"
    assert json.loads(request.data) == {"a": "é"}
    assert request.get_header("Authorization") == "Bearer first-token"
    assert request.get_header("Accept") == "application/json"
    assert request.get_header("Content-type") == "application/json"
    assert timeout == client.TIMEOUT_SECONDS == 10
    assert context is CONTEXT


def test_send_keeps_a_method_its_body_would_not_imply(token):
    api, opener = http(token, (200, {}), (200, {}))
    api.send("PUT", "/p", {"a": 1})
    api.send("DELETE", "/p")
    assert [call[0].get_method() for call in opener.calls] == ["PUT", "DELETE"]


def test_send_without_a_body_sends_no_data_or_content_type(token):
    api, opener = http(token, (200, {"items": []}))
    assert api.send("GET", "/api/v1/pods") == (200, {"items": []})
    request = opener.calls[0][0]
    assert request.get_method() == "GET"
    assert request.data is None
    assert request.get_header("Content-type") is None


def test_send_reads_a_rotated_token_on_every_request(token):
    api, opener = http(token, (200, {}), (200, {}))
    api.send("GET", "/a")
    token.write_text("second-token")
    api.send("GET", "/b")
    assert [call[0].get_header("Authorization") for call in opener.calls] == [
        "Bearer first-token",
        "Bearer second-token",
    ]


def test_send_returns_the_status_and_body_of_an_http_error(token):
    api, _ = http(token, http_error(409, b'{"reason": "AlreadyExists"}'))
    assert api.send("POST", "/p", {}) == (409, {"reason": "AlreadyExists"})


def test_send_returns_an_empty_body_for_an_http_error_without_json(token):
    api, _ = http(token, http_error(502, b"<html>bad gateway</html>"))
    assert api.send("GET", "/p") == (502, {})


def test_send_turns_an_unreachable_server_into_a_connection_error(token):
    api, _ = http(token, urllib.error.URLError("connection refused"))
    with pytest.raises(ConnectionError) as raised:
        api.send("GET", "/p")
    assert str(raised.value) == "connection refused"


class RawOpener(Opener):
    def __call__(self, request, timeout, context):
        self.calls.append((request, timeout, context))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class Truncated:
    def read(self):
        raise IncompleteRead(b"{")


def test_send_turns_an_unreadable_success_body_into_a_connection_error(token):
    opener = RawOpener(Response(201, b"<html>proxy</html>"))
    with pytest.raises(ConnectionError) as raised:
        KubeHttp("https://api.test", token, CONTEXT, opener).send("POST", "/p", {})
    assert str(raised.value) == "the API server answer is unreadable"


def test_send_turns_a_truncated_success_body_into_a_connection_error(token):
    response = Response(200, b"")
    response.read = Truncated().read
    with pytest.raises(ConnectionError) as raised:
        KubeHttp("https://api.test", token, CONTEXT, RawOpener(response)).send("GET", "/p")
    assert str(raised.value) == "the API server exchange failed: IncompleteRead"


def test_send_turns_a_tls_failure_while_reading_into_a_connection_error(token):
    def broken():
        raise ssl.SSLError("record layer failure")

    response = Response(200, b"")
    response.read = broken
    with pytest.raises(ConnectionError) as raised:
        KubeHttp("https://api.test", token, CONTEXT, RawOpener(response)).send("GET", "/p")
    assert str(raised.value) == "the API server exchange failed: SSLError"


def test_send_returns_an_empty_body_when_an_http_error_body_breaks(token):
    def broken():
        raise ssl.SSLError("record layer failure")

    error = http_error(500, b"")
    error.read = broken
    api, _ = http(token, error)
    assert api.send("GET", "/p") == (500, {})


def test_send_lets_a_timeout_while_reading_through(token):
    def slow():
        raise TimeoutError("read timed out")

    response = Response(200, b"")
    response.read = slow
    with pytest.raises(TimeoutError):
        KubeHttp("https://api.test", token, CONTEXT, RawOpener(response)).send("GET", "/p")


def test_send_returns_an_empty_body_for_a_truncated_http_error(token):
    error = http_error(409, b"")
    error.read = Truncated().read
    api, _ = http(token, error)
    assert api.send("POST", "/p", {}) == (409, {})


def test_send_without_a_readable_token_sends_nothing(tmp_path):
    opener = Opener((200, {}))
    with pytest.raises(ConnectionError) as raised:
        KubeHttp("https://api.test", tmp_path / "missing", CONTEXT, opener).send("GET", "/p")
    assert str(raised.value) == "the service account token is unreadable"
    assert opener.calls == []


def test_send_lets_a_read_timeout_through_as_a_timeout(token):
    api, _ = http(token, TimeoutError("timed out"))
    with pytest.raises(TimeoutError):
        api.send("GET", "/p")


def test_in_cluster_reads_the_service_host_and_the_mounted_ca(tmp_path, monkeypatch):
    (tmp_path / "token").write_text("mounted")
    contexts = []
    monkeypatch.setattr(client.ssl, "create_default_context", lambda cafile: contexts.append(cafile) or CONTEXT)
    opener = Opener((200, {}))
    api = KubeHttp.in_cluster(
        {"KUBERNETES_SERVICE_HOST": "10.43.0.1", "KUBERNETES_SERVICE_PORT": "443"}, tmp_path, opener
    )
    api.send("GET", "/version")
    request, _, context = opener.calls[0]
    assert request.full_url == "https://10.43.0.1:443/version"
    assert request.get_header("Authorization") == "Bearer mounted"
    assert contexts == [str(tmp_path / "ca.crt")]
    assert context is CONTEXT


def test_in_cluster_brackets_an_ipv6_service_host(tmp_path, monkeypatch):
    monkeypatch.setattr(client.ssl, "create_default_context", lambda cafile: CONTEXT)
    api = KubeHttp.in_cluster({"KUBERNETES_SERVICE_HOST": "fd00::1", "KUBERNETES_SERVICE_PORT": "6443"}, tmp_path)
    assert api.server == "https://[fd00::1]:6443"


def test_in_cluster_defaults_to_the_service_account_mount_and_urlopen(monkeypatch):
    monkeypatch.setattr(client.ssl, "create_default_context", lambda cafile: cafile)
    api = KubeHttp.in_cluster({"KUBERNETES_SERVICE_HOST": "h", "KUBERNETES_SERVICE_PORT": "1"})
    assert client.ACCOUNT == client.Path("/var/run/secrets/kubernetes.io/serviceaccount")
    assert api.context == str(client.ACCOUNT / "ca.crt")
    assert api.token_path == client.ACCOUNT / "token"
    assert api.opener is client.urllib.request.urlopen


class Http:
    def __init__(self, *answers) -> None:
        self.answers, self.calls = list(answers), []

    def send(self, method, path, body=None):
        self.calls.append((method, path, body))
        return self.answers.pop(0)


def test_create_posts_the_pod_into_its_namespace():
    transport = Http((201, {"metadata": {"uid": "u1"}}))
    pods = PodClient(transport, "swarm-pods")
    assert pods.create_pod({"metadata": {"name": "swarm-a"}}) == {"metadata": {"uid": "u1"}}
    assert transport.calls == [("POST", "/api/v1/namespaces/swarm-pods/pods", {"metadata": {"name": "swarm-a"}})]


def test_create_accepts_a_200_answer():
    assert PodClient(Http((200, {"ok": 1})), "ns").create_pod({"metadata": {"name": "p"}}) == {"ok": 1}


def test_create_raises_already_exists_naming_the_pod():
    pods = PodClient(Http((409, {"reason": "AlreadyExists"})), "ns")
    with pytest.raises(AlreadyExists) as raised:
        pods.create_pod({"metadata": {"name": "swarm-a"}})
    assert str(raised.value) == "swarm-a"


def test_create_refuses_a_conflict_that_is_not_already_exists():
    pods = PodClient(Http((409, {"reason": "Conflict"})), "ns")
    with pytest.raises(ApiRefused) as raised:
        pods.create_pod({"metadata": {"name": "swarm-a"}})
    assert (raised.value.status, raised.value.reason) == (409, "Conflict")
    assert str(raised.value) == "API server refused with 409 Conflict"


@pytest.mark.parametrize("status", [429, 500, 504])
def test_overload_and_server_errors_are_ambiguous(status):
    pods = PodClient(Http((status, {})), "ns")
    with pytest.raises(ConnectionError) as raised:
        pods.create_pod({"metadata": {"name": "p"}})
    assert str(raised.value) == f"API server answered {status}"


@pytest.mark.parametrize("status", [400, 403, 422, 499])
def test_client_errors_are_refusals(status):
    pods = PodClient(Http((status, {"reason": "Forbidden"})), "ns")
    with pytest.raises(ApiRefused) as raised:
        pods.read_pod("p")
    assert (raised.value.status, raised.value.reason) == (status, "Forbidden")


def test_a_refusal_without_a_reason_names_none():
    with pytest.raises(ApiRefused) as raised:
        PodClient(Http((403, {})), "ns").read_pod("p")
    assert raised.value.reason == ""


def test_read_gets_the_named_pod_and_answers_none_when_absent():
    transport = Http((200, {"metadata": {"name": "swarm-a"}}), (404, {"reason": "NotFound"}))
    pods = PodClient(transport, "ns")
    assert pods.read_pod("swarm-a") == {"metadata": {"name": "swarm-a"}}
    assert pods.read_pod("gone") is None
    assert transport.calls == [
        ("GET", "/api/v1/namespaces/ns/pods/swarm-a", None),
        ("GET", "/api/v1/namespaces/ns/pods/gone", None),
    ]


def test_list_selects_by_label_without_a_resource_version():
    transport = Http((200, {"items": [{"metadata": {"name": "a"}}], "metadata": {"resourceVersion": "9"}}))
    pods = PodClient(transport, "ns")
    assert pods.list_pods("owner=x,execution=y") == [{"metadata": {"name": "a"}}]
    assert transport.calls == [("GET", "/api/v1/namespaces/ns/pods?labelSelector=owner%3Dx%2Cexecution%3Dy", None)]


def test_list_raises_on_a_refused_answer():
    with pytest.raises(ApiRefused):
        PodClient(Http((403, {"reason": "Forbidden"})), "ns").list_pods("a=b")


def test_namespace_is_kept_for_callers():
    assert PodClient(Http(), "swarm").namespace == "swarm"
