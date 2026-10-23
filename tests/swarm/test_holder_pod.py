import pytest

from scripts.swarm import holder_pod
from scripts.swarm_v2.kubernetes.client import ApiRefused

pytestmark = pytest.mark.unit

OWN = {"namespace": "swarm", "name": "new", "uid": "uid-new"}
HELD = {"namespace": "other", "name": "old", "uid": "uid-old"}
ENVIRON = {"AGENTIHOOKS_POD_NAMESPACE": "swarm", "AGENTIHOOKS_POD_NAME": "new", "AGENTIHOOKS_POD_UID": "uid-new"}


def statuses(*entries):
    return {"metadata": {"uid": "uid-new"}, "status": {"containerStatuses": list(entries)}}


def test_the_pod_is_read_from_the_downward_api_names_only_when_complete():
    assert holder_pod.pod(ENVIRON) == OWN
    for name in ENVIRON:
        assert holder_pod.pod({**ENVIRON, name: ""}) == {}
        assert holder_pod.pod({key: value for key, value in ENVIRON.items() if key != name}) == {}


def test_the_identity_adds_the_running_container_id_of_the_named_container():
    reads = []
    found = statuses({"name": "jump", "containerID": "c-jump"}, {"name": "controller", "containerID": "c-1"})
    environ = {**ENVIRON, "AGENTIHOOKS_POD_CONTAINER": "controller"}
    assert holder_pod.identity(environ, lambda ns, name: reads.append((ns, name)) or found) == {
        **OWN,
        "container": "c-1",
    }
    assert reads == [("swarm", "new")]


@pytest.mark.parametrize(
    "answer",
    [
        None,
        statuses(),
        statuses({"name": "jump", "containerID": "c-jump"}),
        statuses({"name": "controller"}),
        ApiRefused(403, "Forbidden"),
    ],
)
def test_the_identity_without_a_known_container_id_is_the_pod_alone(answer):
    def read(namespace, name):
        if isinstance(answer, Exception):
            raise answer
        return answer

    assert holder_pod.identity({**ENVIRON, "AGENTIHOOKS_POD_CONTAINER": "controller"}, read) == OWN


def test_the_identity_reads_nothing_without_a_container_name_or_a_pod():
    assert holder_pod.identity(ENVIRON, lambda ns, name: pytest.fail("read")) == OWN
    assert holder_pod.identity({"AGENTIHOOKS_POD_CONTAINER": "controller"}, lambda ns, name: pytest.fail("read")) == {}


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


def running(container_id, state):
    return {
        "metadata": {"uid": "uid-old"},
        "status": {"phase": "Running", "containerStatuses": [{"containerID": container_id, "state": state}]},
    }


@pytest.mark.parametrize(
    ("pod", "gone"),
    [
        (running("c-old", {"running": {}}), False),
        (running("c-old", {"terminated": {}}), True),
        (running("c-old", {"waiting": {}}), True),
        (running("c-new", {"running": {}}), True),
        ({"metadata": {"uid": "uid-old"}, "status": {"phase": "Running"}}, True),
    ],
)
def test_a_holder_container_is_gone_once_it_no_longer_runs_in_its_pod(pod, gone):
    assert holder_pod.gone({**HELD, "container": "c-old"}, OWN, lambda namespace, name: pod) is gone


@pytest.mark.parametrize("container", ["", 7, None])
def test_a_holder_without_a_container_id_is_judged_by_its_pod_alone(container):
    found = running("c-new", {"running": {}})
    assert holder_pod.gone({**HELD, "container": container}, OWN, lambda namespace, name: found) is False


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
        {**HELD, "name": "../../nodes/x"},
        {**HELD, "name": "old?watch=1"},
        {**HELD, "namespace": "Other"},
        {**HELD, "namespace": "-other"},
        {**HELD, "name": "old-"},
        {**HELD, "name": "o" * 254},
        {**HELD, "namespace": "a.b"},
        {**HELD, "namespace": "o" * 64},
    ],
)
def test_an_incomplete_or_unsafe_holder_pod_is_never_read(held):
    assert holder_pod.gone(held, OWN, lambda namespace, name: pytest.fail("read")) is False


@pytest.mark.parametrize("name", ["a", "controller-7f9.x", "o" * 253])
def test_a_dns_named_holder_pod_is_read(name):
    assert holder_pod.gone({**HELD, "name": name}, OWN, lambda namespace, found: None) is True


@pytest.mark.parametrize("namespace", ["a", "agentihooks-swarm", "o" * 63])
def test_a_holder_in_a_dns_label_namespace_is_read(namespace):
    assert holder_pod.gone({**HELD, "namespace": namespace}, OWN, lambda found, name: None) is True


def test_a_recovering_process_outside_a_pod_never_judges():
    assert holder_pod.gone(HELD, {}, lambda namespace, name: pytest.fail("read")) is False


def test_the_pod_is_read_in_cluster_in_the_holders_namespace(monkeypatch):
    from scripts.swarm_v2.kubernetes import client

    seen = []
    monkeypatch.setattr(client.KubeHttp, "in_cluster", classmethod(lambda cls, environ: seen.append(environ) or "http"))
    monkeypatch.setattr(client.PodClient, "read_pod", lambda self, name: (self.http, self.namespace, name))
    assert holder_pod.read_pod("other", "old") == ("http", "other", "old")
    assert seen == [holder_pod.os.environ]
