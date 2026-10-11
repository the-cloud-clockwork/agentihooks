import json

import pytest

from scripts.swarm.store import AgentRecord
from scripts.swarm_v2.auth_context import GrantRefused, Registration
from scripts.swarm_v2.broadcast_bridge import GRANT_NAME
from scripts.swarm_v2.hand import Hand
from scripts.swarm_v2.kubernetes.client import AlreadyExists, ApiRefused, PreconditionFailed
from scripts.swarm_v2.kubernetes.grants import PodGrants, Supervision
from scripts.swarm_v2.kubernetes.spec import LAUNCH_DIR, LAUNCH_RECORD, PodTemplate, launch_name
from scripts.swarm_v2.supervision import Launch
from tests import sv2_kub02_cases as cases

pytestmark = pytest.mark.unit

EXECUTION = "exe-0f1e2d3c4b5a69788796a5b4c3d2e1f0"
POD = f"swarm-{EXECUTION}"
SLUG = "fixture"
OWNER = "agentihooks-swarm-fixture"
NAMESPACE = "swarm-pod-proof"
GRANT = "sv2.grant-fixture"
GRANT_ID = "lgr-" + "7" * 32
CONTROL = "http://controller.swarm.invalid:8780"
AUTHORITY = {
    "execution_id": EXECUTION,
    "generation": 3,
    "task_id": "t1",
    "seat_id": f"eng-1@{SLUG}",
    "swarm_id": SLUG,
    "grant_id": GRANT_ID,
}
RECORD = {
    "schema_version": 1,
    "execution_id": EXECUTION,
    "generation": 3,
    "authority": AUTHORITY,
    "harness": "claude",
    "agent": ["claude"],
    "control_url": CONTROL,
}
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


def registration() -> Registration:
    return Registration(
        **AUTHORITY,
        account="claude-fixture",
        brain_id="swarm",
        project_ids=["github.com/the-cloud-clockwork/agentihooks"],
        issuer="swarm",
        audience="workers",
        key_id="launch-1",
        registered_at="",
    )


def verify(token: str) -> Registration:
    if token != GRANT:
        raise GrantRefused("unauthenticated", "launch grant was not issued by this controller")
    return registration()


def grants(api, exporter=None) -> PodGrants:
    return PodGrants(api, SLUG, verify, Supervision("claude", exporter, CONTROL))


def material(record=None, **changes) -> dict:
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
        "data": {"launch-grant": GRANT, "launch.json": json.dumps(record or RECORD, sort_keys=True)},
    }
    return {**body, **changes}


def test_the_grant_lands_in_the_launch_config_map_its_pod_owns():
    api = Api(pod())

    assert grants(api).hand(agent(), GRANT) == Hand(True, "handed", False)

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


def test_the_launch_record_sits_where_the_pod_template_starts_the_supervisor():
    rendered = PodTemplate(cases.policy()).render(cases.launch()).pod
    args = rendered["spec"]["containers"][0]["args"]

    assert args[1] == "/opt/swarm-node/supervisor.py"
    assert args[-1] == f"{LAUNCH_DIR}/{LAUNCH_RECORD}" == "/var/run/swarm/launch/launch.json"


def test_the_config_map_holds_the_grant_and_the_launch_record_beside_it():
    api = Api(pod())

    assert grants(api).hand(agent(), GRANT) == Hand(True, "handed", False)

    [created] = api.created
    assert sorted(created["data"]) == [GRANT_NAME, LAUNCH_RECORD]
    assert created["data"][GRANT_NAME] == GRANT
    assert json.loads(created["data"][LAUNCH_RECORD]) == RECORD


def test_a_set_exporter_is_copied_into_the_launch_record():
    api = Api(pod())

    assert grants(api, ("python", "-m", "exporter")).hand(agent(), GRANT) == Hand(True, "handed", False)

    assert api.created == [material({**RECORD, "exporter": ["python", "-m", "exporter"]})]


def test_the_supervisor_loads_the_handed_record_against_the_registered_authority(tmp_path):
    api = Api(pod())
    grants(api).hand(agent(), GRANT)
    attempt = tmp_path / EXECUTION
    (attempt / "homes" / "claude").mkdir(parents=True)
    (attempt / "execution.json").write_text(json.dumps({"attempt": EXECUTION, "homes": {"claude": "homes/claude"}}))
    (attempt / "registration.json").write_text(json.dumps(AUTHORITY))
    path = tmp_path / LAUNCH_RECORD
    path.write_text(api.created[0]["data"][LAUNCH_RECORD])

    launch = Launch.load(attempt, path)

    assert (launch.authority, launch.harness, launch.agent, launch.exporter) == (AUTHORITY, "claude", ("claude",), None)


def test_a_grant_the_controller_did_not_issue_takes_no_config_map():
    api = Api(pod())

    assert grants(api).hand(agent(), "sv2.forged") == Hand(False, "grant_unverified", False)

    assert (api.reads, api.created) == ([], [])


def test_a_grant_the_store_cannot_check_takes_no_config_map():
    from redis.exceptions import ConnectionError as RedisDown

    def unreachable(token):
        raise RedisDown("store unreachable")

    api = Api(pod())

    assert PodGrants(api, SLUG, unreachable, Supervision("claude", None, CONTROL)).hand(agent(), GRANT) == Hand(
        False, "grant_unverified", False
    )

    assert (api.reads, api.created) == ([], [])


def test_a_missing_pod_takes_no_grant():
    api = Api(None)

    assert grants(api).hand(agent(), GRANT) == Hand(False, "pod_missing", False)
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

    assert grants(api).hand(agent(), GRANT) == Hand(False, "pod_foreign", False)
    assert api.created == []
    assert api.deleted == []


def test_a_pod_without_labels_takes_no_grant():
    found = pod()
    del found["metadata"]["labels"]
    api = Api(found)

    assert grants(api).hand(agent(), GRANT) == Hand(False, "pod_foreign", False)
    assert api.created == []
    assert api.deleted == []


def test_a_deleting_pod_takes_no_grant():
    api = Api(pod(deletionTimestamp="2026-10-10T20:00:00Z"))

    assert grants(api).hand(agent(), GRANT) == Hand(False, "pod_foreign", False)
    assert api.created == []
    assert api.deleted == []


@pytest.mark.parametrize(
    "failure", [ApiRefused(403, "Forbidden"), ConnectionError("API server answered 503"), TimeoutError("read")]
)
def test_an_unreadable_pod_takes_no_grant(failure):
    api = Api(pod(), failure=failure)

    assert grants(api).hand(agent(), GRANT) == Hand(False, "pod_unreadable", False)
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

    assert grants(api).hand(agent(), GRANT) == Hand(False, reason, True)
    assert api.deleted == [("pods", POD, "uid-1")]


def test_a_pod_already_gone_after_a_refused_config_map_counts_as_removed():
    api = Api(pod(), create_failure=ApiRefused(403, "Forbidden"), delete_answer=False)

    assert grants(api).hand(agent(), GRANT) == Hand(False, "config_map_refused", True)
    assert api.deleted == [("pods", POD, "uid-1")]


@pytest.mark.parametrize(
    "answer", [PreconditionFailed(POD), ApiRefused(403, "Forbidden"), ConnectionError("API server answered 503")]
)
def test_a_pod_that_cannot_be_removed_after_a_refused_config_map_says_so(answer):
    api = Api(pod(), create_failure=ApiRefused(403, "Forbidden"), delete_answer=answer)

    assert grants(api).hand(agent(), GRANT) == Hand(False, "config_map_refused", False)
    assert api.deleted == [("pods", POD, "uid-1")]
