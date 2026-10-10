import pytest

from scripts.swarm.store import AgentRecord
from scripts.swarm_v2.kubernetes import grants
from scripts.swarm_v2.kubernetes.client import AlreadyExists, ApiRefused
from scripts.swarm_v2.kubernetes.grants import PodGrants
from scripts.swarm_v2.kubernetes.spec import LAUNCH_DIR, PodTemplate, launch_name
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
    def __init__(self, found=None, failure=None, create_failure=None) -> None:
        self.namespace, self.found = NAMESPACE, found
        self.failure, self.create_failure = failure, create_failure
        self.reads, self.created = [], []

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

    assert PodGrants(api, SLUG).hand(agent(), GRANT) is True

    assert api.reads == [POD]
    assert api.created == [material()]


def test_the_config_map_is_the_one_the_pod_template_mounts_at_the_launch_folder():
    rendered = PodTemplate(cases.policy()).render(cases.launch()).pod
    [volume] = [volume for volume in rendered["spec"]["volumes"] if volume["name"] == "launch"]
    [mount] = [mount for mount in rendered["spec"]["containers"][0]["volumeMounts"] if mount["name"] == "launch"]

    assert volume["configMap"]["name"] == material()["metadata"]["name"] == launch_name(EXECUTION)
    assert f"{mount['mountPath']}/{grants.GRANT_KEY}" == f"{LAUNCH_DIR}/launch-grant"
    assert mount["readOnly"] is True


def test_a_missing_pod_takes_no_grant():
    api = Api(None)

    assert PodGrants(api, SLUG).hand(agent(), GRANT) is False
    assert api.created == []


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

    assert PodGrants(api, SLUG).hand(agent(), GRANT) is False
    assert api.created == []


def test_a_pod_without_labels_takes_no_grant():
    found = pod()
    del found["metadata"]["labels"]
    api = Api(found)

    assert PodGrants(api, SLUG).hand(agent(), GRANT) is False
    assert api.created == []


def test_a_deleting_pod_takes_no_grant():
    api = Api(pod(deletionTimestamp="2026-10-10T20:00:00Z"))

    assert PodGrants(api, SLUG).hand(agent(), GRANT) is False
    assert api.created == []


@pytest.mark.parametrize(
    "failure", [ApiRefused(403, "Forbidden"), ConnectionError("API server answered 503"), TimeoutError("read")]
)
def test_an_unreadable_pod_takes_no_grant(failure):
    api = Api(pod(), failure=failure)

    assert PodGrants(api, SLUG).hand(agent(), GRANT) is False
    assert api.created == []


@pytest.mark.parametrize(
    "failure",
    [
        AlreadyExists(f"swarm-{EXECUTION}-launch"),
        ApiRefused(403, "Forbidden"),
        ConnectionError("API server answered 503"),
        TimeoutError("read"),
    ],
)
def test_a_config_map_that_exists_or_is_refused_is_not_handed(failure):
    api = Api(pod(), create_failure=failure)

    assert PodGrants(api, SLUG).hand(agent(), GRANT) is False


def test_the_grant_never_reaches_a_repr_or_the_owner_name():
    grants_ = PodGrants(Api(pod()), SLUG)

    assert grants_.owner == OWNER
    assert GRANT not in repr(grants_)
