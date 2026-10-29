import pytest

from scripts.swarm_v2 import retention
from tests import sv2_kub04_cases as cases
from tests.test_swarm_v2_kubernetes_cleanup import ready as ready


def refused(world, old):
    name = f"swarm-{old.execution_id}"
    world.api.objects[name]["status"] = {
        "phase": "Failed",
        "reason": "Error",
        "containerStatuses": [
            {
                "name": "agent",
                "state": {"terminated": {"exitCode": 78, "reason": "Error", "message": "startup refused"}},
            }
        ],
    }
    return name


def test_refused_startup_log_and_exit_reason_remain_readable_after_cleanup(ready):
    world, controller, old, _ = ready
    name = refused(world, old)

    def logs(pod, container):
        assert pod == name and container == "agent"
        assert name in world.api.objects
        return "worker startup refused: profile binding mismatch\n"

    world.api.pod_logs = logs
    assert world.cleanup(controller).run(old.execution_id).state == "done"
    assert name not in world.api.objects
    kept = retention.retained(world.store, cases.SLUG, old.execution_id)
    startup = kept["removed"]["pods"][0]["startup"]
    assert startup["phase"] == "Failed"
    assert startup["reason"] == "Error"
    assert startup["containers"] == [
        {
            "name": "agent",
            "terminated": {"exitCode": 78, "reason": "Error", "message": "startup refused"},
            "log": "worker startup refused: profile binding mismatch\n",
        }
    ]


def test_unreadable_startup_logs_keep_the_pod_for_retry(ready):
    world, controller, old, _ = ready
    name = refused(world, old)

    def unavailable(pod, container):
        raise ConnectionError("logs unavailable")

    world.api.pod_logs = unavailable
    with pytest.raises(ConnectionError, match="logs unavailable"):
        world.cleanup(controller).run(old.execution_id)
    assert name in world.api.objects
    assert retention.retained(world.store, cases.SLUG, old.execution_id) is None


def test_logs_are_persisted_before_delete_and_reused_after_a_refused_delete(ready):
    import json

    world, controller, old, _ = ready
    name = refused(world, old)
    world.api.pod_logs = lambda pod, container: "startup refused\n"
    delete = world.api.delete

    def interrupted(kind, pod, uid):
        journal = json.loads(world.store.redis.hget(world.store.key(cases.SLUG, "cleanup-journal"), old.execution_id))
        assert journal["pinned"]["pods"][0]["startup"]["containers"][0]["log"] == "startup refused\n"
        raise ConnectionError("delete unavailable")

    world.api.delete = interrupted
    with pytest.raises(ConnectionError, match="delete unavailable"):
        world.cleanup(controller).run(old.execution_id)
    assert name in world.api.objects
    world.api.delete = delete

    def unreadable(pod, container):
        raise AssertionError("persisted logs must not be fetched twice")

    world.api.pod_logs = unreadable
    assert world.cleanup(controller).run(old.execution_id).state == "done"
    assert retention.retained(world.store, cases.SLUG, old.execution_id)["removed"]["pods"][0]["startup"]


@pytest.mark.parametrize("status", [200, 403, 404])
def test_pod_log_requests_are_bounded_and_refusals_raise(status):
    from urllib.parse import parse_qs, urlsplit

    from scripts.swarm_v2.kubernetes.client import ApiRefused, PodClient

    class Http:
        def send(self, method, path, body=None, read=None):
            assert method == "GET" and body is None
            parsed = urlsplit(path)
            assert parsed.path == "/api/v1/namespaces/workers/pods/refused/log"
            assert parse_qs(parsed.query) == {
                "container": ["agent"],
                "tailLines": ["200"],
                "limitBytes": ["65536"],
                "timestamps": ["true"],
            }
            return (status, read(b"startup refused\xff\n")) if status == 200 else (status, {})

    client = PodClient(Http(), "workers")
    if status == 200:
        assert client.pod_logs("refused", "agent") == "startup refused\ufffd\n"
    else:
        with pytest.raises(ApiRefused):
            client.pod_logs("refused", "agent")


@pytest.mark.parametrize("persist", [True, False])
def test_terminal_setup_refusal_retains_started_worker_diagnostics_before_removal(ready, persist):
    import json
    from functools import partial

    from scripts.swarm_v2.kubernetes.diagnostics import retain
    from scripts.swarm_v2.kubernetes.grants import PodGrants, Supervision
    from tests import test_swarm_v2_kubernetes_grants as grants

    world, _, old, _ = ready
    api = grants.Api(grants.pod())
    original = api.delete

    class Terminals:
        def open(self, registration, pod):
            pod["status"] = {
                "phase": "Failed",
                "containerStatuses": [{"name": "agent", "state": {"terminated": {"exitCode": 78, "reason": "Error"}}}],
            }
            return "terminal_refused"

    def delete(kind, name, uid):
        saved = json.loads(
            world.store.redis.hget(world.store.key(grants.SLUG, "attempt-startup-diagnostics"), grants.EXECUTION)
        )
        assert saved["uid"] == "uid-1"
        assert saved["startup"]["containers"][0]["log"] == "worker startup refused\n"
        return original(kind, name, uid)

    api.delete = delete
    api.pod_logs = lambda pod, container: "worker startup refused\n"
    hand = PodGrants(
        api,
        grants.SLUG,
        grants.verify,
        Supervision("claude", None, grants.CONTROL),
        Terminals(),
        retain=partial(retain, world.store, grants.SLUG) if persist else None,
    ).hand(grants.agent(), grants.GRANT)
    assert hand.reason == "terminal_refused"
    assert hand.removed is persist


@pytest.mark.parametrize("section,phase", [("initContainerStatuses", "Failed"), ("containerStatuses", "Running")])
def test_init_and_nonterminal_container_refusals_keep_their_exit_reason(ready, section, phase):
    world, controller, old, _ = ready
    name = refused(world, old)
    status = world.api.objects[name]["status"]
    status["phase"] = phase
    status[section] = status.pop("containerStatuses")
    world.api.pod_logs = lambda pod, container: "startup refused\n"
    assert world.cleanup(controller).run(old.execution_id).state == "done"
    saved = retention.retained(world.store, cases.SLUG, old.execution_id)["removed"]["pods"][0]["startup"]
    assert saved["containers"][0]["terminated"]["exitCode"] == 78


def test_healthy_cleanup_does_not_request_startup_logs(ready):
    world, controller, old, _ = ready

    def unexpected(pod, container):
        raise AssertionError("healthy cleanup requested refusal logs")

    world.api.pod_logs = unexpected
    assert world.cleanup(controller).run(old.execution_id).state == "done"
    assert "startup" not in retention.retained(world.store, cases.SLUG, old.execution_id)["removed"]["pods"][0]


class Logs:
    def __init__(self) -> None:
        self.requested = []

    def pod_logs(self, name, container):
        self.requested.append((name, container))
        return "startup refused\n"


def status_pod(status=None):
    pod = {"metadata": {"name": "refused"}}
    if status is not None:
        pod["status"] = status
    return pod


@pytest.mark.parametrize(
    "pod",
    [
        status_pod(),
        status_pod({"phase": "Running", "containerStatuses": [{"name": "agent"}]}),
        status_pod({"phase": "Succeeded", "containerStatuses": [{"name": "agent", "state": {"terminated": {}}}]}),
        status_pod(
            {"phase": "Running", "containerStatuses": [{"name": "agent", "state": {"terminated": {"exitCode": 0}}}]}
        ),
    ],
)
def test_pods_without_a_failed_phase_or_nonzero_exit_are_not_captured(pod):
    from scripts.swarm_v2.kubernetes.diagnostics import startup

    logs = Logs()
    assert startup(pod, logs) is None
    assert logs.requested == []


def test_a_failed_phase_alone_captures_terminated_containers_with_a_clean_exit():
    from scripts.swarm_v2.kubernetes.diagnostics import startup

    logs = Logs()
    pod = status_pod(
        {
            "phase": "Failed",
            "containerStatuses": [{"name": "agent", "state": {"terminated": {"exitCode": 0}}}, {"name": "idle"}],
        }
    )
    assert startup(pod, logs) == {
        "phase": "Failed",
        "reason": "",
        "containers": [{"name": "agent", "terminated": {"exitCode": 0}, "log": "startup refused\n"}],
    }
    assert logs.requested == [("refused", "agent")]


def test_a_nonzero_exit_without_a_phase_is_captured_with_an_empty_phase():
    from scripts.swarm_v2.kubernetes.diagnostics import startup

    pod = status_pod({"containerStatuses": [{"name": "agent", "state": {"terminated": {"exitCode": 1}}}]})
    assert startup(pod, Logs())["phase"] == ""


def test_retained_startup_records_carry_the_pod_identity_and_labels(ready):
    import json

    from scripts.swarm_v2.kubernetes.diagnostics import retain
    from scripts.swarm_v2.kubernetes.watch import EXECUTION_LABEL

    world, _, _, _ = ready
    labels = {EXECUTION_LABEL: "exe-1", "owner": "fixture"}
    retain(
        world.store,
        cases.SLUG,
        {"metadata": {"name": "refused", "uid": "uid-1", "labels": labels}},
        {"phase": "Failed"},
    )
    saved = world.store.redis.hget(world.store.key(cases.SLUG, "attempt-startup-diagnostics"), "exe-1")
    assert json.loads(saved) == {"pod": "refused", "uid": "uid-1", "labels": labels, "startup": {"phase": "Failed"}}


@pytest.mark.parametrize("gone,removed", [(True, True), (False, False)])
def test_launch_removal_checks_the_current_pod_before_deleting(gone, removed):
    from tests import test_swarm_v2_kubernetes_grants as grants

    found = grants.pod(project="github.com/other/project")
    later = None if gone else grants.pod(project="github.com/other/project", uid="uid-2")
    api = grants.Api(found)
    reads = iter([found, later])
    api.read_pod = lambda name: next(reads)
    hand = grants.grants(api).hand(grants.agent(), grants.GRANT)
    assert (hand.reason, hand.removed) == ("project_unpermitted", removed)
    assert api.deleted == []
