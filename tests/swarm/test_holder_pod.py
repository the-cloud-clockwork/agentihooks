import pytest

from scripts.swarm import holder_pod
from scripts.swarm_v2.kubernetes.client import ApiRefused

pytestmark = pytest.mark.unit

OWN = {"namespace": "swarm", "name": "new", "uid": "uid-new"}
HELD = {"namespace": "other", "name": "old", "uid": "uid-old"}


def test_the_identity_is_read_from_the_downward_api_names_only_when_complete():
    environ = {"AGENTIHOOKS_POD_NAMESPACE": "swarm", "AGENTIHOOKS_POD_NAME": "new", "AGENTIHOOKS_POD_UID": "uid-new"}
    assert holder_pod.identity(environ) == OWN
    for name in environ:
        assert holder_pod.identity({**environ, name: ""}) == {}
        assert holder_pod.identity({key: value for key, value in environ.items() if key != name}) == {}


@pytest.mark.parametrize(
    ("pod", "gone"),
    [
        (None, True),
        ({"metadata": {"uid": "uid-other"}, "status": {"phase": "Running"}}, True),
        ({"metadata": {"uid": "uid-old"}, "status": {"phase": "Failed"}}, True),
        ({"metadata": {"uid": "uid-old"}, "status": {"phase": "Succeeded"}}, True),
        ({"metadata": {"uid": "uid-old"}, "status": {"phase": "Running"}}, False),
        ({"metadata": {"uid": "uid-old"}, "status": {"phase": "Unknown"}}, False),
        ({"metadata": {"uid": "uid-old"}}, False),
        ({"status": {"phase": "Running"}}, True),
    ],
)
def test_a_holder_pod_is_gone_only_when_missing_replaced_or_ended(pod, gone):
    reads = []
    assert holder_pod.gone(HELD, OWN, lambda namespace, name: reads.append((namespace, name)) or pod) is gone
    assert reads == [("other", "old")]


@pytest.mark.parametrize(
    "error",
    [ApiRefused(403, "Forbidden"), KeyError("KUBERNETES_SERVICE_HOST"), ConnectionError("down"), TimeoutError()],
)
def test_an_unreadable_holder_pod_is_never_gone(error):
    def read(namespace, name):
        raise error

    assert holder_pod.gone(HELD, OWN, read) is False


@pytest.mark.parametrize(
    "held",
    [
        None,
        "pod",
        {},
        {"namespace": "other", "name": "old"},
        {"namespace": "other", "name": "old", "uid": ""},
        {**HELD, "uid": 7},
    ],
)
def test_an_incomplete_holder_pod_is_never_read(held):
    assert holder_pod.gone(held, OWN, lambda namespace, name: pytest.fail("read")) is False


def test_a_holder_outside_a_pod_never_judges_and_this_pod_judges_without_reading():
    assert holder_pod.gone(HELD, {}, lambda namespace, name: pytest.fail("read")) is False
    assert holder_pod.gone({**HELD, "uid": "uid-new"}, OWN, lambda namespace, name: pytest.fail("read")) is True


def test_the_pod_is_read_in_cluster_in_the_holders_namespace(monkeypatch):
    from scripts.swarm_v2.kubernetes import client

    seen = []
    monkeypatch.setattr(client.KubeHttp, "in_cluster", classmethod(lambda cls, environ: seen.append(environ) or "http"))
    monkeypatch.setattr(client.PodClient, "read_pod", lambda self, name: (self.http, self.namespace, name))
    assert holder_pod.read_pod("other", "old") == ("http", "other", "old")
    assert seen == [holder_pod.os.environ]
