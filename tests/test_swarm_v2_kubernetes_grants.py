import pytest

from scripts.swarm.store import AgentRecord
from scripts.swarm_v2.broadcast_bridge import GRANT_NAME
from scripts.swarm_v2.kubernetes.client import AlreadyExists, ApiRefused, PreconditionFailed
from scripts.swarm_v2.kubernetes.grants import PodGrants
from scripts.swarm_v2.kubernetes.spec import LAUNCH_DIR, PodTemplate, launch_name
from scripts.swarm_v2.launch import Hand
from tests import sv2_kub02_cases as cases

pytestmark = pytest.mark.unit

EXECUTION = "exe-0f1e2d3c4b5a69788796a5b4c3d2e1f0"
POD = f"swarm-{EXECUTION}"
SLUG = "fixture"
OWNER = "agentihooks-swarm-fixture"
NAMESPACE = "swarm-pod-proof"
GRANT = "sv2.grant-fixture"
LABELS = {
    "swarm.agentihooks.io/controller-owner": OWNER,
    "swarm.agentihooks.io/execution-id": EXECUTION,
    "swarm.agentihooks.io/generation": "3",
}


def agent(execution_id=EXECUTION, generation=3) -> AgentRecord:
    return AgentRecord("engineer-1", "eng", "t1", execution_id=execution_id, generation=generation)


def pod(labels=None, **metadata) -> dict:
    return {
        "metadata": {
            "name": POD,
            "namespace": NAMESPACE,
            "uid": "uid-1",
            "labels": LABELS if labels is None else labels,
            **metadata,
        }
    }


class Api:
    def __init__(self, found=None, failure=None, create_failure=None, delete_answer=True) -> None:
        self.namespace, self.found = NAMESPACE, found
        self.failure, self.create_failure, self.delete_answer = failure, create_failure, delete_answer
        self.reads, self.created, self.deleted = [], [], []

    def read_pod(self, name):
        self.reads.append(name)
        if self.failure is not None:
            raise self.failure
        return self.found

    def create_config_map(self, body):
        if self.create_failure is not None:
            raise self.create_failure
        self.created.append(body)
        return body

    def delete(self, kind, name, uid):
        self.deleted.append((kind, name, uid))
        if isinstance(self.delete_answer, Exception):
            raise self.delete_answer
        return self.delete_answer


def material(**changes) -> dict:
    body = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": f"swarm-{EXECUTION}-launch",
            "namespace": NAMESPACE,
            "labels": LABELS,
            "ownerReferences": [{"apiVersion": "v1", "kind": "Pod", "name": POD, "uid": "uid-1"}],
        },
        "immutable": True,
        "data": {"launch-grant": GRANT},
    }
    return {**body, **changes}


def test_the_grant_lands_in_the_launch_config_map_its_pod_owns():
    api = Api(pod())

    assert PodGrants(api, SLUG).hand(agent(), GRANT) == Hand(True, "handed", False)

    assert api.reads == [POD]
    assert api.created == [material()]
    assert api.deleted == []


def test_the_config_map_is_the_one_the_pod_template_mounts_at_the_launch_folder():
    rendered = PodTemplate(cases.policy()).render(cases.launch()).pod
    [volume] = [volume for volume in rendered["spec"]["volumes"] if volume["name"] == "launch"]
    [mount] = [mount for mount in rendered["spec"]["containers"][0]["volumeMounts"] if mount["name"] == "launch"]

    assert volume["configMap"]["name"] == material()["metadata"]["name"] == launch_name(EXECUTION)
    assert f"{mount['mountPath']}/{GRANT_NAME}" == f"{LAUNCH_DIR}/launch-grant"
    assert mount["readOnly"] is True


def test_a_missing_pod_takes_no_grant():
    api = Api(None)

    assert PodGrants(api, SLUG).hand(agent(), GRANT) == Hand(False, "pod_missing", False)
    assert api.created == []
    assert api.deleted == []


@pytest.mark.parametrize(
    "labels",
    [
        {**LABELS, "swarm.agentihooks.io/controller-owner": "agentihooks-swarm-other"},
        {**LABELS, "swarm.agentihooks.io/execution-id": "exe-" + "9" * 32},
        {**LABELS, "swarm.agentihooks.io/generation": "4"},
        {key: value for key, value in LABELS.items() if key != "swarm.agentihooks.io/generation"},
        {},
    ],
)
def test_a_pod_another_execution_or_controller_owns_takes_no_grant(labels):
    api = Api(pod(labels))

    assert PodGrants(api, SLUG).hand(agent(), GRANT) == Hand(False, "pod_foreign", False)
    assert api.created == []
    assert api.deleted == []


def test_a_pod_without_labels_takes_no_grant():
    found = pod()
    del found["metadata"]["labels"]
    api = Api(found)

    assert PodGrants(api, SLUG).hand(agent(), GRANT) == Hand(False, "pod_foreign", False)
    assert api.created == []
    assert api.deleted == []


def test_a_deleting_pod_takes_no_grant():
    api = Api(pod(deletionTimestamp="2026-10-10T20:00:00Z"))

    assert PodGrants(api, SLUG).hand(agent(), GRANT) == Hand(False, "pod_foreign", False)
    assert api.created == []
    assert api.deleted == []


@pytest.mark.parametrize(
    "failure", [ApiRefused(403, "Forbidden"), ConnectionError("API server answered 503"), TimeoutError("read")]
)
def test_an_unreadable_pod_takes_no_grant(failure):
    api = Api(pod(), failure=failure)

    assert PodGrants(api, SLUG).hand(agent(), GRANT) == Hand(False, "pod_unreadable", False)
    assert api.created == []
    assert api.deleted == []


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (AlreadyExists(f"swarm-{EXECUTION}-launch"), "config_map_exists"),
        (ApiRefused(403, "Forbidden"), "config_map_refused"),
        (ConnectionError("API server answered 503"), "config_map_unavailable"),
        (TimeoutError("read"), "config_map_unavailable"),
    ],
)
def test_a_config_map_that_exists_or_is_refused_removes_its_pod_and_names_why(failure, reason):
    api = Api(pod(), create_failure=failure)

    assert PodGrants(api, SLUG).hand(agent(), GRANT) == Hand(False, reason, True)
    assert api.deleted == [("pods", POD, "uid-1")]


@pytest.mark.parametrize(
    "answer",
    [False, PreconditionFailed(POD), ApiRefused(403, "Forbidden"), ConnectionError("API server answered 503")],
)
def test_a_pod_that_cannot_be_removed_after_a_refused_config_map_says_so(answer):
    api = Api(pod(), create_failure=ApiRefused(403, "Forbidden"), delete_answer=answer)

    assert PodGrants(api, SLUG).hand(agent(), GRANT) == Hand(False, "config_map_refused", False)
    assert api.deleted == [("pods", POD, "uid-1")]
