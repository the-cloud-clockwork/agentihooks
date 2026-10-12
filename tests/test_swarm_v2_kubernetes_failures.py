import json
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from scripts.swarm import lease
from scripts.swarm.store import SwarmError
from scripts.swarm_v2 import deployed, retention
from scripts.swarm_v2.kubernetes import client, failures, replacement
from scripts.swarm_v2.kubernetes.failures import AccountSlot, Decision
from scripts.swarm_v2.runtime import observe
from scripts.swarm_v2.runtime.base import Outcome, Status
from tests import sv2_kub05_cases as cases

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-KUB-05"
POD = {"metadata": {}, "spec": {}, "status": {}}


@pytest.fixture
def world():
    world = cases.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        world.live = controller
        world.old = world.launch(controller, world.fx["first"])
        yield world


def _pod(status: dict, node: str = "") -> dict:
    return {"metadata": {}, "spec": {"nodeName": node} if node else {}, "status": status}


def _container(**state) -> dict:
    return {"phase": "Running", "containerStatuses": [{"name": "agent", "state": state}]}


def test_reason_names_and_pull_states_are_fixed():
    assert failures.REASONS == ("oom_killed", "evicted", "node_lost", "image_pull", "application_exit")
    assert failures.PULL_WAITING == frozenset(("ErrImagePull", "ImagePullBackOff", "InvalidImageName"))
    assert failures.RELEASES == ("grant", "account")
    assert failures.PULL_GRACE_MS == 120_000
    assert failures.NODE_NOT_READY == "NodeNotReady"
    assert (failures.AWAITING, failures.FENCED) == ("awaiting-decision", "fenced")


@pytest.mark.parametrize(
    ("shape", "reason"),
    [
        ("oom_killed", "oom_killed"),
        ("evicted", "evicted"),
        ("node_unreachable", "node_lost"),
        ("node_lost", "node_lost"),
        ("image_pull", "image_pull"),
        ("init_image_pull", "image_pull"),
        ("application_exit", "application_exit"),
        ("clean_exit", ""),
        ("running", ""),
    ],
)
def test_each_fixture_shape_classifies_by_its_own_signal(shape, reason):
    assert failures.classify(cases.fixture()["pods"][shape], frozenset({"worker-b"})) == reason


def test_node_health_counts_only_when_observed():
    pod = cases.fixture()["pods"]["running"]
    assert failures.classify(pod) == ""
    assert failures.classify(pod, frozenset({"worker-b"})) == ""
    assert failures.classify(pod, frozenset()) == "node_lost"
    assert failures.classify(_pod({"phase": "Pending"}), frozenset()) == ""
    assert failures.classify(cases.fixture()["pods"]["node_unreachable"]) == "node_lost"
    lost = {"phase": "Running", "conditions": [{"type": "Ready", "status": "False", "reason": "NodeNotReady"}]}
    assert failures.classify(_pod(lost, "worker-b"), frozenset({"worker-b"})) == "node_lost"
    unready = {"phase": "Running", "conditions": [{"type": "Ready", "status": "False", "reason": "ContainersNotReady"}]}
    assert failures.classify(_pod(unready, "worker-b")) == ""


@pytest.mark.parametrize(
    ("condition", "reason"),
    [
        ({"type": "DisruptionTarget", "status": "True", "reason": "DeletionByTaintManager"}, "node_lost"),
        ({"type": "DisruptionTarget", "status": "True", "reason": "DeletionByPodGC"}, "node_lost"),
        ({"type": "DisruptionTarget", "status": "False", "reason": "DeletionByTaintManager"}, ""),
        ({"type": "DisruptionTarget", "status": "True", "reason": "PreemptionByScheduler"}, ""),
        ({"type": "DisruptionTarget", "status": "True"}, ""),
        ({"type": "Ready", "status": "True", "reason": "DeletionByTaintManager"}, ""),
        ({"type": "Ready", "status": "False", "reason": "NodeLost"}, "node_lost"),
    ],
)
def test_a_lost_node_is_read_from_namespaced_pod_conditions(condition, reason):
    status = {"phase": "Running", "conditions": [condition]}
    assert failures.classify(_pod(status, "worker-b")) == reason
    assert failures.classify(_pod(status, "worker-b"), frozenset({"worker-b"})) == reason
    assert failures.NODE_DISRUPTIONS == frozenset(("DeletionByTaintManager", "DeletionByPodGC"))


def test_a_deleted_pod_is_judged_by_its_last_state_then_by_its_last_node():
    finished = _pod({"phase": "Succeeded"}, "worker-b")
    assert failures.deleted_reason(finished, None) == ""
    assert failures.deleted_reason(finished, frozenset({"worker-b"})) == ""
    assert failures.deleted_reason(finished, frozenset()) == "node_lost"
    assert failures.deleted_reason({"metadata": {}, "spec": {"nodeName": "worker-b"}}, None) == "node_lost"
    assert failures.deleted_reason({"metadata": {}, "status": {"phase": "Running"}}, None) == ""
    assert failures.deleted_reason({"metadata": {}}, None) == ""


def test_init_containers_and_a_missing_exit_code_count_as_exits():
    init = {"phase": "Failed", "initContainerStatuses": [{"name": "home", "state": {"terminated": {}}}]}
    assert failures.classify(_pod(init)) == "application_exit"
    init["initContainerStatuses"][0]["state"]["terminated"] = {"reason": "OOMKilled", "exitCode": 137}
    assert failures.classify(_pod(init)) == "oom_killed"
    pending = {"phase": "Pending", "initContainerStatuses": [{"state": {"terminated": {"exitCode": 1}}}]}
    assert failures.classify(_pod(pending)) == "application_exit"
    assert failures.classify(_pod(_container(terminated={"reason": "Error"}))) == "application_exit"


def test_oom_outranks_eviction_node_loss_and_a_failed_phase():
    status = {"phase": "Failed", "reason": "Evicted", **_container(terminated={"reason": "OOMKilled"})}
    assert failures.classify(_pod(status, "gone"), frozenset()) == "oom_killed"
    assert failures.classify(_pod({"phase": "Failed", "reason": "Evicted"}, "gone"), frozenset()) == "evicted"


def test_a_pull_wait_or_a_non_zero_exit_in_any_container_counts():
    for reason in sorted(failures.PULL_WAITING):
        assert failures.classify(_pod(_container(waiting={"reason": reason}))) == "image_pull"
    assert failures.classify(_pod(_container(waiting={"reason": "ContainerCreating"}))) == ""
    assert failures.classify(_pod(_container(terminated={"reason": "Error", "exitCode": 2}))) == "application_exit"
    assert failures.classify(_pod(_container(terminated={"reason": "Completed", "exitCode": 0}))) == ""
    assert failures.classify(_pod({"phase": "Failed"})) == "application_exit"
    assert failures.classify(POD) == ""
    assert failures.classify({}) == ""


def test_tail_explains_the_unarchived_transcript_and_the_image_case():
    assert failures.tail("image_pull", "s", 9) == failures.NO_TAIL
    assert failures.tail("node_lost", "sess-1", 4096) == (
        "transcript written by native session sess-1 after archive offset 4096 was not archived before the "
        "node lost failure and is not recovered; its length is unknown"
    )
    assert "native session unknown " in failures.tail("oom_killed", "", 0)


def test_failure_counts_start_at_zero_for_every_reason(world):
    assert world.recovery(world.live).execution_failures_by_reason() == dict.fromkeys(failures.REASONS, 0)


def test_the_fence_is_written_before_any_release_or_replacement(world):
    recovery, order = world.recovery(world.live), []
    admit = world.live.admit

    def admitted(record, previous_execution_id=""):
        order.append(("admit", recovery.fence(world.old.execution_id)["released"], list(world.log)))
        return admit(record, previous_execution_id)

    world.live.admit = admitted
    decision = recovery.handle(world.old.execution_id, "oom_killed")
    assert order == [("admit", ["grant", "account"], ["grant"])]
    fence = recovery.fence(world.old.execution_id)
    assert fence == {
        "execution_id": world.old.execution_id,
        "generation": 1,
        "seat": world.old.seat,
        "reason": "oom_killed",
        "controller_epoch": world.live.held.epoch,
        "fenced_at_ms": world.clock[0],
        "native_session": world.fx["first"]["native_session"],
        "archive_watermark": world.fx["first"]["archive_watermark"],
        "tail": failures.tail("oom_killed", world.fx["first"]["native_session"], 4096),
        "released": ["grant", "account"],
    }
    assert decision == Decision(world.old.execution_id, 1, "oom_killed", "resume", "ckpt-one-2", decision.replacement)
    assert world.store.execution(cases.SLUG, decision.replacement).generation == 2


def test_handling_again_returns_the_recorded_decision_without_side_effects(world):
    recovery = world.recovery(world.live)
    first = recovery.handle(world.old.execution_id, "evicted")
    before = world.protected()
    assert recovery.handle(world.old.execution_id, "oom_killed") == first
    assert world.protected() == before
    assert recovery.execution_failures_by_reason()["evicted"] == 1


def test_only_the_current_attempt_of_a_seat_can_be_fenced(world):
    recovery = world.recovery(world.live)
    replacement = recovery.handle(world.old.execution_id, "oom_killed").replacement
    newer = world.recovery(world.live)
    world.store.redis.hdel(world.store.key(cases.SLUG, "attempt-fences"), world.old.execution_id)
    world.store.redis.hdel(world.store.key(cases.SLUG, "attempt-recoveries"), world.old.execution_id)
    with pytest.raises(SwarmError, match="^only the current attempt of a seat can be fenced$"):
        newer.handle(world.old.execution_id, "oom_killed")
    assert world.occupant().execution_id == replacement


def test_a_seat_moved_past_the_fenced_attempt_is_refused(world):
    recovery = world.recovery(world.live)
    recovery.handle(world.old.execution_id, "oom_killed")
    second = recovery.decision(world.old.execution_id).replacement
    third = world.live.admit(replace(world.store.execution(cases.SLUG, second), execution_id="", generation=0), second)
    assert third.generation == 3
    world.store.redis.hdel(world.store.key(cases.SLUG, "attempt-recoveries"), world.old.execution_id)
    with pytest.raises(SwarmError, match="^the seat moved past the fenced attempt$"):
        recovery.handle(world.old.execution_id, "oom_killed")


def test_a_paused_decision_after_replacement_leaves_the_successor_alone(world):
    recovery = world.recovery(world.live)
    successor = recovery.handle(world.old.execution_id, "oom_killed").replacement
    assert world.store.execution(cases.SLUG, world.old.execution_id).state == "fenced"
    world.store.redis.hdel(world.store.key(cases.SLUG, "attempt-recoveries"), world.old.execution_id)
    paused = world.recovery(world.live, automatic=False).handle(world.old.execution_id, "oom_killed")
    assert (paused.mode, paused.replacement) == ("recovery_pending", "")
    assert world.occupant().execution_id == successor
    assert world.occupant().state == "working"


def test_a_lost_fence_race_keeps_the_winning_fence(world, monkeypatch):
    recovery = world.recovery(world.live)
    winner = {
        "execution_id": world.old.execution_id,
        "generation": 1,
        "seat": world.old.seat,
        "reason": "evicted",
        "controller_epoch": world.live.held.epoch,
        "fenced_at_ms": 7,
        "native_session": "",
        "archive_watermark": 0,
        "tail": "won elsewhere",
        "released": [],
    }
    hset = recovery.store.redis.hset

    def raced(key, field, value):
        hset(key, field, json.dumps(winner))
        return False

    monkeypatch.setattr(recovery.store.redis, "hsetnx", raced)
    decision = recovery.handle(world.old.execution_id, "oom_killed")
    assert decision.reason == "evicted"
    assert recovery.fence(world.old.execution_id) == {**winner, "released": ["grant", "account"]}
    assert recovery.execution_failures_by_reason() == {**dict.fromkeys(failures.REASONS, 0), "evicted": 1}


def test_unfinished_fences_resume_in_execution_order(world):
    other = world.launch(world.live, world.fx["second"], "eng-2")
    recovery, order = world.recovery(world.live), []
    for attempt in (other, world.old):
        recovery._fence(world.store.execution(cases.SLUG, attempt.execution_id), "oom_killed")
    admit = world.live.admit

    def admitted(record, previous_execution_id=""):
        order.append(previous_execution_id)
        return admit(record, previous_execution_id)

    world.live.admit = admitted
    world.reconcile(recovery)
    assert order == sorted([other.execution_id, world.old.execution_id])


def test_an_operator_pause_outlives_the_recovery_instance(world):
    world.recovery(world.live).pause(True)
    restarted = world.recovery(world.live)
    assert restarted.paused()
    decision = restarted.handle(world.old.execution_id, "oom_killed")
    assert (decision.mode, decision.replacement) == ("recovery_pending", "")
    restarted.pause(False)
    assert not world.recovery(world.live).paused()
    other = world.launch(world.live, world.fx["second"], "eng-2")
    assert world.recovery(world.live).handle(other.execution_id, "oom_killed").mode == "resume"
    world.grant["allowed"] = False
    with pytest.raises(SwarmError, match="a scoped controller grant is required"):
        restarted.pause(True)
    assert not restarted.paused()


def test_a_node_seen_before_a_controller_restart_still_fences_its_vanished_pod(world):
    assert world.reconcile(world.recovery(world.live)) == {world.old.execution_id: "working"}
    world.clock[0] += cases.kub02.lease.ttl_ms()
    restarted, _ = world.controller()
    assert restarted.acquire()
    world.ready.discard(world.fx["first"]["node"])
    world.api.objects.pop(f"swarm-{world.old.execution_id}")
    assert world.reconcile(world.recovery(restarted)) == {world.old.execution_id: "fenced"}


def test_the_latest_complete_compatible_checkpoint_wins(world):
    recovery = world.recovery(world.live)
    world.checkpoints.by_execution[world.old.execution_id] = [
        {"checkpoint_id": "b", "sequence": 5, "status": "complete", "compatibility": world.fx["compatibility"]},
        {"checkpoint_id": "a", "sequence": 9, "status": "complete", "compatibility": world.fx["compatibility"]},
        {"checkpoint_id": "c", "sequence": 3, "status": "complete", "compatibility": world.fx["compatibility"]},
        {"checkpoint_id": "d", "sequence": 12, "status": "partial", "compatibility": world.fx["compatibility"]},
        {"checkpoint_id": "e", "sequence": 15, "status": "complete", "compatibility": "other"},
    ]
    assert recovery.handle(world.old.execution_id, "application_exit").checkpoint == "a"


def test_no_checkpoint_or_a_paused_recovery_leaves_the_seat_awaiting_a_decision(world):
    world.checkpoints.by_execution[world.old.execution_id] = []
    decision = world.recovery(world.live).handle(world.old.execution_id, "node_lost")
    assert (decision.mode, decision.replacement, decision.checkpoint) == ("recovery_pending", "", "")
    assert world.occupant().state == "awaiting-decision"
    other = world.launch(world.live, world.fx["second"], "eng-2")
    paused = world.recovery(world.live, automatic=False).handle(other.execution_id, "oom_killed")
    assert (paused.mode, paused.replacement) == ("recovery_pending", "")
    assert world.occupant("eng-2").state == "awaiting-decision"
    assert len(world.attempts("eng-2")) == 1


def test_pending_lists_only_the_attempts_still_awaiting_a_decision(world):
    world.checkpoints.by_execution[world.old.execution_id] = []
    recovery = world.recovery(world.live)
    assert recovery.pending() == []
    other = world.launch(world.live, world.fx["second"], "eng-2")
    recovery.handle(world.old.execution_id, "node_lost")
    world.recovery(world.live, automatic=False).handle(other.execution_id, "oom_killed")
    assert recovery.pending() == sorted([world.old.execution_id, other.execution_id])
    recovery.decide(world.old.execution_id, "fresh")
    assert recovery.pending() == [other.execution_id]


def test_an_image_pull_never_replaces_automatically(world):
    decision = world.recovery(world.live).handle(world.old.execution_id, "image_pull")
    assert decision.mode == "recovery_pending"
    assert world.attempts() == [[world.old.execution_id, 1]]


def test_a_container_without_a_state_shows_no_failure():
    status = {"phase": "Pending", "initContainerStatuses": [{"name": "init"}], "containerStatuses": [{"name": "a"}]}
    assert failures.classify(_pod(status)) == ""


def test_recovery_replaces_automatically_unless_told_otherwise(world):
    built = world.recovery(world.live)
    default = failures.Recovery(
        built.store, built.slug, built.controller, built.checkpoints, built.releases, built.compatibility
    )
    assert default.handle(world.old.execution_id, "oom_killed").mode == "resume"


def test_pods_without_an_execution_label_are_ignored(world):
    recovery = world.recovery(world.live)
    pods = [*world.api.objects.values(), {"metadata": {}}, {"metadata": {"labels": {"app": "other"}}}]
    assert recovery.reconcile(pods, sorted(world.ready)) == {world.old.execution_id: "working"}


def test_a_pod_without_a_spec_is_observed_without_a_node(world):
    recovery = world.recovery(world.live)
    pod = json.loads(json.dumps(world.api.objects[f"swarm-{world.old.execution_id}"]))
    del pod["spec"]
    assert recovery.reconcile([pod], sorted(world.ready)) == {world.old.execution_id: "working"}
    assert world.store.redis.hget(world.store.key(cases.SLUG, "attempt-nodes"), world.old.execution_id) is None


def test_a_live_pod_on_a_node_outside_the_ready_set_is_lost(world):
    recovery = world.recovery(world.live)
    world.ready.discard(world.fx["first"]["node"])
    assert world.reconcile(recovery) == {world.old.execution_id: "fenced"}
    assert recovery.fence(world.old.execution_id)["reason"] == "node_lost"


def test_each_reappearing_old_pod_counts_as_one_late_observation(world):
    recovery = world.recovery(world.live)
    pod = world.api.objects[f"swarm-{world.old.execution_id}"]
    recovery.handle(world.old.execution_id, "oom_killed")
    recovery.reconcile([pod, json.loads(json.dumps(pod))], sorted(world.ready))
    assert recovery.late_observations(world.old.execution_id) == 2


def test_a_settled_decision_is_not_reopened_by_a_later_failure(world):
    recovery = world.recovery(world.live, automatic=False)
    recovery.handle(world.old.execution_id, "evicted")
    settled = recovery.decide(world.old.execution_id, "fresh")
    assert recovery.handle(world.old.execution_id, "oom_killed") == settled
    assert len(world.attempts()) == 2


def test_the_replacement_starts_on_a_pending_pod_whatever_the_old_target(world):
    old = world.store.execution(cases.SLUG, world.old.execution_id)
    bound = replace(old, runtime_target={**old.runtime_target, "pod_name": "swarm-old"})
    replacement = world.store.execution(cases.SLUG, world.recovery(world.live)._replace(bound).execution_id)
    assert replacement.runtime_target == {"pod_namespace": world.api.namespace, "pod_name": "swarm-pending"}


def test_recovery_reads_the_clock_of_its_own_store(world):
    def clock(store):
        assert store.redis.time()
        return world.clock[0]

    recovery = world.recovery(world.live)
    world.show(world.old, "image_pull")
    with mock.patch.object(failures.lease, "now_ms", clock):
        assert world.reconcile(recovery) == {world.old.execution_id: "pulling"}
        recovery.handle(world.old.execution_id, "image_pull")
    assert recovery.fence(world.old.execution_id)["fenced_at_ms"] == world.clock[0]


def test_the_pause_records_the_controller_epoch_that_set_it(world):
    recovery = world.recovery(world.live)
    recovery.pause(True)
    key = world.store.key(cases.SLUG, "recovery-paused")
    assert int(world.store.redis.get(key)) == world.live.held.epoch


class _Nodes:
    def __init__(self, answer):
        self.answer, self.paths = answer, []

    def send(self, method, path, body=None):
        self.paths.append((method, path))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _node(name, *conditions):
    return {"metadata": {"name": name}, "status": {"conditions": list(conditions)}}


def test_only_ready_nodes_are_listed_and_a_failed_listing_is_none():
    ready, unready = {"type": "Ready", "status": "True"}, {"type": "Ready", "status": "False"}
    items = [
        _node("a", {"type": "MemoryPressure", "status": "True"}, ready),
        _node("b", unready),
        _node("c", {"type": "DiskPressure", "status": "True"}),
        {"metadata": {"name": "d"}, "status": {}},
        {"metadata": {"name": "e"}},
    ]
    http = _Nodes((200, {"items": items}))
    assert client.PodClient(http, "swarm").ready_nodes() == ["a"]
    assert http.paths == [("GET", "/api/v1/nodes")]
    for answer in ((403, {"reason": "Forbidden"}), (201, {"items": items}), ConnectionError("down")):
        assert client.PodClient(_Nodes(answer), "swarm").ready_nodes() is None


class _Grants:
    def __init__(self):
        self.revoked, self.verified = [], []

    def verify(self, slug, token):
        self.verified.append((slug, token))

    def revoke(self, slug, execution_id):
        self.revoked.append((slug, execution_id))
        return True


class _Cluster:
    def __init__(self, world, nodes=True):
        self.world, self.nodes, self.selectors, self.watches, self.events, self.version = world, nodes, [], [], [], 0

    def snapshot(self, selector):
        self.selectors.append(selector)
        self.version += 1
        return json.loads(json.dumps(list(self.world.api.objects.values()))), str(self.version)

    def watch_pods(self, selector, version):
        self.watches.append((selector, version))
        events, self.events = self.events, []
        return events

    def ready_nodes(self):
        return sorted(self.world.ready) if self.nodes else None

    def __getattr__(self, name):
        if name in ("namespace", "list_pods", "list_services", "read", "delete"):
            return getattr(self.world.api, name)
        raise AttributeError(name)


def test_the_deployed_pass_fences_an_attempt_whose_node_was_deleted(world):
    grants, cluster = _Grants(), _Cluster(world)
    service = SimpleNamespace(controller=world.live, grants=grants)
    recover = deployed.recovery_pass(service, SimpleNamespace(image_tag="dev"), cluster)
    assert recover.recovery.compatibility == "dev"
    recover.recovery.releases["account"].capacity.authorize("a-token")
    assert grants.verified == [(cases.SLUG, "a-token")]
    assert recover() == {world.old.execution_id: "working"}
    world.ready.discard(world.fx["first"]["node"])
    world.api.objects.pop(f"swarm-{world.old.execution_id}")
    assert recover() == {world.old.execution_id: "fenced"}
    fence = world.recovery(world.live).fence(world.old.execution_id)
    assert (fence["reason"], fence["released"]) == ("node_lost", ["grant", "account"])
    assert "before the node lost failure and is not recovered" in fence["tail"]
    decision = world.recovery(world.live).decision(world.old.execution_id)
    assert (decision.mode, decision.replacement) == ("recovery_pending", "")
    assert world.occupant().state == "awaiting-decision"
    assert grants.revoked == [(cases.SLUG, world.old.execution_id)]
    assert world.slots() == []
    assert cluster.selectors == [f"{cases.watch.OWNER_LABEL}={cases.watch.owner_for(cases.SLUG)}"] * 2


def test_the_deployed_pass_fences_a_pod_only_the_watch_saw_deleted_while_nodes_are_refused(world):
    grants, cluster = _Grants(), _Cluster(world, nodes=False)
    service = SimpleNamespace(controller=world.live, grants=grants)
    recover = deployed.recovery_pass(service, SimpleNamespace(image_tag="dev"), cluster)
    last = _gone(world)
    assert recover() == {world.old.execution_id: "unobserved"}
    assert cluster.watches == []
    cluster.events = [("DELETED", last)]
    assert recover() == {world.old.execution_id: "fenced"}
    selector = f"{cases.watch.OWNER_LABEL}={cases.watch.owner_for(cases.SLUG)}"
    assert cluster.watches == [(selector, "1")]
    fence = world.recovery(world.live).fence(world.old.execution_id)
    assert (fence["reason"], fence["released"]) == ("node_lost", ["grant", "account"])
    assert grants.revoked == [(cases.SLUG, world.old.execution_id)]


def _gone(world):
    return world.api.objects.pop(f"swarm-{world.old.execution_id}")


def _listed(world):
    return json.loads(json.dumps(list(world.api.objects.values())))


def test_a_deleted_pod_never_seen_live_is_fenced_as_node_lost_without_a_node_listing(world):
    recovery = world.recovery(world.live)
    last = _gone(world)
    assert recovery.reconcile([], None, [last]) == {world.old.execution_id: "fenced"}
    fence = recovery.fence(world.old.execution_id)
    assert (fence["reason"], fence["released"]) == ("node_lost", ["grant", "account"])
    assert world.slots() == []
    decision = recovery.decision(world.old.execution_id)
    replacement = world.store.execution(cases.SLUG, decision.replacement)
    assert (decision.mode, replacement.generation, replacement.seat) == ("resume", 2, world.old.seat)
    assert recovery.reconcile([], None, [last]) == {decision.replacement: "unobserved"}
    assert recovery.late_observations(world.old.execution_id) == 0


@pytest.mark.parametrize(("shape", "reason"), [("oom_killed", "oom_killed"), ("image_pull", "image_pull")])
def test_a_deleted_pod_keeps_the_failure_its_last_state_shows(world, shape, reason):
    recovery = world.recovery(world.live)
    world.show(world.old, shape)
    last = _gone(world)
    assert recovery.reconcile([], None, [last]) == {world.old.execution_id: "fenced"}
    assert recovery.fence(world.old.execution_id)["reason"] == reason


def test_a_finished_or_unscheduled_deleted_pod_is_not_a_node_loss(world):
    recovery = world.recovery(world.live)
    world.show(world.old, "clean_exit")
    finished = _gone(world)
    unscheduled = {**finished, "spec": {}, "status": {"phase": "Pending"}}
    for last in (finished, unscheduled):
        assert recovery.reconcile([], None, [last]) == {world.old.execution_id: "unobserved"}
    assert recovery.fence(world.old.execution_id) is None


def test_a_finished_deleted_pod_on_a_node_the_listing_no_longer_shows_is_node_lost(world):
    recovery = world.recovery(world.live)
    world.show(world.old, "clean_exit")
    last = _gone(world)
    assert recovery.reconcile([], ["worker-a"], [last]) == {world.old.execution_id: "fenced"}
    assert recovery.fence(world.old.execution_id)["reason"] == "node_lost"


def test_a_completed_attempt_is_never_fenced_when_its_pod_and_node_are_gone(world):
    recovery = world.recovery(world.live)
    world.show(world.old, "clean_exit")
    retention.finalize(world.store, cases.SLUG, world.live.require, world.old.execution_id, "completed", 0, "ref")
    last = _gone(world)
    assert recovery.reconcile([], ["worker-a"], [last]) == {world.old.execution_id: "completed"}
    assert recovery.fence(world.old.execution_id) is None


class _Cleaning(_Cluster):
    def __getattr__(self, name):
        return getattr(self.world.api, name)


def test_the_deployed_pass_deletes_a_completed_attempt_pod_and_scale_down_never_fences_it(world):
    grants, cluster = _Grants(), _Cleaning(world)
    service = SimpleNamespace(controller=world.live, grants=grants)
    recover = deployed.recovery_pass(service, SimpleNamespace(image_tag="dev"), cluster)
    node = world.fx["first"]["node"]
    world.show(world.old, "clean_exit", node)
    assert recover() == {world.old.execution_id: "working"}
    assert f"swarm-{world.old.execution_id}" not in world.api.objects
    assert grants.revoked == [(cases.SLUG, world.old.execution_id)]
    world.ready.discard(node)
    assert recover() == {world.old.execution_id: "completed"}
    assert world.recovery(world.live).fence(world.old.execution_id) is None


def test_the_deployed_pass_keeps_a_running_attempt_pod(world):
    recover = deployed.recovery_pass(
        SimpleNamespace(controller=world.live, grants=_Grants()), SimpleNamespace(image_tag="dev"), _Cleaning(world)
    )
    assert recover() == {world.old.execution_id: "working"}
    assert f"swarm-{world.old.execution_id}" in world.api.objects
    assert world.api.deletes == []


def test_a_live_pod_outranks_a_deleted_state_of_the_same_attempt(world):
    recovery = world.recovery(world.live)
    live = _listed(world)
    last = {**live[0], "metadata": {**live[0]["metadata"], "uid": "older"}}
    assert recovery.reconcile(live, None, [last]) == {world.old.execution_id: "working"}
    assert recovery.fence(world.old.execution_id) is None


def test_deleted_states_of_unknown_attempts_are_ignored(world):
    recovery = world.recovery(world.live)
    stray = {"metadata": {"labels": {cases.watch.EXECUTION_LABEL: "exe-stray"}}, "spec": {"nodeName": "worker-a"}}
    unlabelled = {"metadata": {}, "spec": {"nodeName": "worker-a"}}
    assert recovery.reconcile(_listed(world), None, [stray, unlabelled]) == {world.old.execution_id: "working"}
    assert recovery.fence("exe-stray") is None


def test_a_refused_node_listing_never_takes_an_unlisted_node_as_proof(world):
    recovery = world.recovery(world.live)
    assert recovery.reconcile(_listed(world), None) == {world.old.execution_id: "working"}
    world.ready.clear()
    assert recovery.reconcile(_listed(world), None) == {world.old.execution_id: "working"}
    _gone(world)
    assert recovery.reconcile([], None) == {world.old.execution_id: "unobserved"}
    assert recovery.fence(world.old.execution_id) is None


class _Recording:
    def __init__(self):
        self.calls = []

    def reconcile(self, pods, ready, deleted):
        self.calls.append((pods, ready, deleted))
        return {}


class _Scripted:
    def __init__(self, snapshots, watches):
        self.snapshots, self.watches, self.calls = list(snapshots), list(watches), []

    def snapshot(self, selector):
        self.calls.append(("list", selector))
        return self.snapshots.pop(0)

    def watch_pods(self, selector, version):
        self.calls.append(("watch", selector, version))
        answer = self.watches.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def ready_nodes(self):
        return None


def _uid(uid, node=""):
    return {"metadata": {"uid": uid}, "spec": {"nodeName": node} if node else {}}


def test_each_pass_hands_recovery_the_listed_pods_and_every_pod_deleted_since_the_last_pass(monkeypatch):
    printed = []
    monkeypatch.setattr(deployed, "print", lambda *line, **flags: printed.append((line, flags)), raising=False)
    a, b, c, d, e = (_uid(uid) for uid in "abcde")
    b_last, c_last = _uid("b", "worker-a"), _uid("c", "worker-b")
    refused = client.ApiRefused(403, "Forbidden")
    events = [("DELETED", c_last), ("MODIFIED", a), ("BOOKMARK", {"metadata": {}}), ("DELETED", b_last)]
    api = _Scripted(
        [([a, b], "5"), ([a], "9"), ([d], "11"), ([e], "12"), ([], "13"), ([], "14")],
        [events, cases.watch.CursorExpired(), refused, ConnectionError("down"), TimeoutError()],
    )
    recording = _Recording()
    recover = deployed.RecoveryPass(recording, api, "owner=x")
    for _ in range(6):
        assert recover() == {}
    assert [deleted for _, _, deleted in recording.calls] == [[], [c_last, b_last], [a], [d], [e], []]
    assert [pods for pods, _, _ in recording.calls] == [[a, b], [a], [d], [e], [], []]
    assert {ready for _, ready, _ in recording.calls} == {None}
    watched = [call for call in api.calls if call[0] == "watch"]
    assert watched == [("watch", "owner=x", version) for version in ("5", "9", "11", "12", "13")]
    assert api.calls[:3] == [("list", "owner=x"), ("list", "owner=x"), ("watch", "owner=x", "5")]
    failed = ("CursorExpired", "ApiRefused", "ConnectionError", "TimeoutError")
    assert printed == [
        ((f"controller: the Pod watch failed with {name}; using the last listing",), {"flush": True}) for name in failed
    ]


def test_a_pod_deleted_after_the_listing_is_reported_by_the_overlapping_watch():
    a = _uid("a")
    a_last = _uid("a", "worker-a")
    api = _Scripted([([a], "5"), ([a], "9"), ([], "12")], [[("DELETED", a_last)], [("DELETED", a_last)]])
    recording = _Recording()
    recover = deployed.RecoveryPass(recording, api, "owner=x")
    for _ in range(3):
        recover()
    assert [deleted for _, _, deleted in recording.calls] == [[], [a_last], [a_last]]
    assert [call for call in api.calls if call[0] == "watch"] == [("watch", "owner=x", "5"), ("watch", "owner=x", "9")]


def test_the_chooser_runs_after_reconcile_and_before_replacements_and_its_lines_reach_the_log(monkeypatch):
    printed, order = [], []
    monkeypatch.setattr(deployed, "print", lambda *line, **flags: printed.append((line, flags)), raising=False)
    recording = _Recording()
    recording.reconcile = lambda *args: order.append("reconcile") or {}
    recover = deployed.RecoveryPass(recording, _Scripted([([], "5")], []), "owner=x")
    recover.choose = lambda: order.append("choose") or ["controller: chose fresh"]
    recover.replacements = lambda: order.append("replace") or {}
    recover()
    assert order == ["reconcile", "choose", "replace"]
    assert printed == [(("controller: chose fresh",), {"flush": True})]


def test_the_deployed_pass_chooses_only_when_given_the_controller_environment(world, monkeypatch):
    service = SimpleNamespace(controller=world.live, grants=_Grants())
    cluster = _Cluster(world)
    assert deployed.recovery_pass(service, SimpleNamespace(image_tag="dev"), cluster).choose is None
    seen = []
    monkeypatch.setattr(deployed.proof_swarm, "choose_fresh", lambda environ, recovery: seen.append(environ) or [])
    recover = deployed.recovery_pass(service, SimpleNamespace(image_tag="dev"), cluster, environ={"A": "1"})
    assert recover.choose() == []
    assert seen == [{"A": "1"}]


def test_the_deployed_pass_fences_a_live_pod_on_a_lost_node_while_nodes_are_refused(world):
    cluster = _Cluster(world, nodes=False)
    service = SimpleNamespace(controller=world.live, grants=_Grants())
    recover = deployed.recovery_pass(service, SimpleNamespace(image_tag="dev"), cluster)
    assert recover() == {world.old.execution_id: "working"}
    pod = world.api.objects[f"swarm-{world.old.execution_id}"]
    lost = {"type": "DisruptionTarget", "status": "True", "reason": "DeletionByTaintManager"}
    pod["status"] = {**pod["status"], "conditions": [lost]}
    assert recover() == {world.old.execution_id: "fenced"}
    assert world.recovery(world.live).fence(world.old.execution_id)["reason"] == "node_lost"


def test_a_pass_without_a_resource_version_lists_again_instead_of_watching():
    a = _uid("a")
    api = _Scripted([([a], ""), ([], "")], [])
    recording = _Recording()
    recover = deployed.RecoveryPass(recording, api, "owner=x")
    recover()
    recover()
    assert api.calls == [("list", "owner=x"), ("list", "owner=x")]
    assert recording.calls[1] == ([], None, [a])


def test_resume_decision_uses_the_checkpoint_and_admits_one_replacement(world):
    world.checkpoints.by_execution[world.old.execution_id] = []
    recovery = world.recovery(world.live)
    recovery.handle(world.old.execution_id, "evicted")
    world.checkpoints.by_execution[world.old.execution_id] = world.fx["first"]["checkpoints"]
    decision = recovery.decide(world.old.execution_id, "resume")
    assert (decision.mode, decision.checkpoint) == ("resume", "ckpt-one-2")
    replacement = world.store.execution(cases.SLUG, decision.replacement)
    assert (replacement.generation, replacement.state, replacement.conversation_id) == (2, "working", "")
    assert replacement.runtime_target == {"pod_namespace": world.api.namespace, "pod_name": "swarm-pending"}
    assert recovery.decision(world.old.execution_id) == decision


def test_each_recovery_event_reads_the_generation_holding_the_seat(world):
    world.checkpoints.by_execution[world.old.execution_id] = []
    recovery = world.recovery(world.live)
    recovery.handle(world.old.execution_id, "evicted")
    world.checkpoints.by_execution[world.old.execution_id] = world.fx["first"]["checkpoints"]
    decision = recovery.decide(world.old.execution_id, "resume")

    rows = [
        (e["mode"], e["expected_generation"], e["observed_generation"], e.get("replacement", ""))
        for e in world.live.telemetry.events(transition="recovery")
    ]
    assert rows == [("recovery_pending", 1, 1, ""), ("resume", 1, 2, decision.replacement)]


def test_a_pull_that_recovers_restarts_its_grace(world):
    recovery = world.recovery(world.live)
    world.show(world.old, "image_pull")
    start = world.clock[0]
    assert world.reconcile(recovery) == {world.old.execution_id: "pulling"}
    world.show(world.old, "running")
    world.clock[0] = start + failures.PULL_GRACE_MS
    assert world.live.renew()
    assert world.reconcile(recovery) == {world.old.execution_id: "working"}
    world.show(world.old, "image_pull")
    assert world.reconcile(recovery) == {world.old.execution_id: "pulling"}
    world.clock[0] += failures.PULL_GRACE_MS - 1
    assert world.live.renew()
    assert world.reconcile(recovery) == {world.old.execution_id: "pulling"}
    world.clock[0] += 1
    assert world.reconcile(recovery) == {world.old.execution_id: "fenced"}


def test_a_vanished_pod_on_a_ready_node_or_an_unseen_node_is_not_a_failure(world):
    recovery = world.recovery(world.live)
    world.api.objects.pop(f"swarm-{world.old.execution_id}")
    assert world.reconcile(recovery) == {world.old.execution_id: "unobserved"}
    world.store.redis.hdel(world.store.key(cases.SLUG, "attempt-nodes"), world.old.execution_id)
    world.ready.clear()
    assert world.reconcile(recovery) == {world.old.execution_id: "unobserved"}
    assert recovery.fence(world.old.execution_id) is None


def test_a_failed_node_listing_judges_no_pod_lost(world):
    recovery = world.recovery(world.live)
    pods = list(world.api.objects.values())
    assert recovery.reconcile(pods, None) == {world.old.execution_id: "working"}
    world.api.objects.pop(f"swarm-{world.old.execution_id}")
    assert recovery.reconcile([], None) == {world.old.execution_id: "unobserved"}
    assert recovery.reconcile([], []) == {world.old.execution_id: "fenced"}


def _observed(world, attempt, *ages_s, mode=observe.Mode.STANDARD, **seen):
    observer, now = observe.Observer(world.store, "kubernetes", observe.Thresholds(mode=mode)), world.clock[0] / 1000
    kind = {"source": observe.Source.KUBERNETES, "reading": observe.Reading.NOT_FOUND, **seen}
    for age in ages_s:
        signal = observe.Signal(
            kind["source"], kind["reading"], now + age, attempt.execution_id, attempt.generation, kind.get("value", "")
        )
        record = observer.observe(cases.SLUG, attempt, [signal], now + age)
    return record


def _stored_for_generation(world, attempt, generation):
    record, now = None, world.clock[0] / 1000
    for age in (0, 600):
        signal = observe.Signal(
            observe.Source.KUBERNETES, observe.Reading.NOT_FOUND, now + age, attempt.execution_id, generation
        )
        record = observe.classify(attempt.execution_id, generation, [signal], record, observe.Thresholds(), now + age)
    world.store.redis.hset(
        world.store.key(cases.SLUG, "observations"), attempt.execution_id, json.dumps(asdict(record))
    )
    return record


def _vanish(world):
    world.api.objects.pop(f"swarm-{world.old.execution_id}")
    world.store.redis.hdel(world.store.key(cases.SLUG, "attempt-nodes"), world.old.execution_id)
    return world.recovery(world.live)


def test_a_pod_vanished_unwatched_is_fenced_as_node_lost_once_its_observation_settles_lost(world):
    recovery = _vanish(world)
    assert _observed(world, world.old, 0, 600).state is observe.State.LOST
    assert world.reconcile(recovery) == {world.old.execution_id: "fenced"}
    assert recovery.fence(world.old.execution_id)["reason"] == "node_lost"


def test_a_pod_vanished_unwatched_stays_unobserved_while_its_observation_is_suspect(world):
    recovery = _vanish(world)
    assert _observed(world, world.old, 0, 599).state is observe.State.SUSPECT
    assert world.reconcile(recovery) == {world.old.execution_id: "unobserved"}
    assert recovery.fence(world.old.execution_id) is None


@pytest.mark.parametrize("step", [-1, 1])
def test_a_lost_observation_of_another_generation_never_fences(world, step):
    recovery = _vanish(world)
    assert _stored_for_generation(world, world.old, world.old.generation + step).state is observe.State.LOST
    assert world.reconcile(recovery) == {world.old.execution_id: "unobserved"}
    assert recovery.fence(world.old.execution_id) is None


@pytest.mark.parametrize(
    "seen",
    [
        {"reading": observe.Reading.OK, "value": "Failed"},
        {"reading": observe.Reading.OK, "value": "Succeeded"},
        {"source": observe.Source.SUPERVISOR, "reading": observe.Reading.OK, "value": "exited"},
    ],
    ids=["failed", "succeeded", "supervisor-exited"],
)
def test_a_lost_observation_proven_by_an_ended_worker_never_fences_a_vanished_pod(world, seen):
    recovery = _vanish(world)
    assert _observed(world, world.old, 0, 600, **seen).state is observe.State.LOST
    assert world.reconcile(recovery) == {world.old.execution_id: "unobserved"}
    assert recovery.fence(world.old.execution_id) is None


def test_an_operator_lost_ruling_fences_a_vanished_pod_and_a_working_ruling_does_not(world):
    recovery, conservative = _vanish(world), observe.Mode.CONSERVATIVE
    held = _observed(world, world.old, 0, 600, mode=conservative)
    assert (held.state, held.needs_operator) == (observe.State.SUSPECT, True)
    now = world.clock[0] / 1000 + 601
    observe.rule(world.store, cases.SLUG, world.old.execution_id, "working", "pod moved", "operator", now)
    assert world.reconcile(recovery) == {world.old.execution_id: "unobserved"}
    _observed(world, world.old, 602, mode=conservative)
    observe.rule(world.store, cases.SLUG, world.old.execution_id, "lost", "pod gone", "operator", now + 2)
    assert world.reconcile(recovery) == {world.old.execution_id: "fenced"}
    assert recovery.fence(world.old.execution_id)["reason"] == "node_lost"


def test_two_live_pods_for_one_attempt_are_left_alone(world):
    recovery = world.recovery(world.live)
    pod = json.loads(json.dumps(world.api.objects[f"swarm-{world.old.execution_id}"]))
    pod["metadata"]["name"] = "copy"
    world.api.objects["copy"] = pod
    world.show(world.old, "oom_killed")
    assert world.reconcile(recovery) == {world.old.execution_id: "ambiguous"}
    assert recovery.fence(world.old.execution_id) is None


def test_late_observations_of_a_fenced_attempt_are_counted_and_ignored(world):
    world.checkpoints.by_execution[world.old.execution_id] = []
    recovery = world.recovery(world.live)
    world.show(world.old, "oom_killed")
    assert world.reconcile(recovery) == {world.old.execution_id: "fenced"}
    world.show(world.old, "running")
    assert world.reconcile(recovery) == {world.old.execution_id: "retired"}
    assert recovery.late_observations(world.old.execution_id) == 1
    world.api.objects.pop(f"swarm-{world.old.execution_id}")
    assert world.reconcile(recovery) == {world.old.execution_id: "retired"}
    assert recovery.late_observations(world.old.execution_id) == 1
    assert recovery.late_observations("exe-none") == 0
    assert world.occupant().state == "awaiting-decision"


def test_reconcile_skips_pods_of_unknown_unfenced_attempts(world):
    recovery = world.recovery(world.live)
    stranger = json.loads(json.dumps(world.api.objects[f"swarm-{world.old.execution_id}"]))
    stranger["metadata"]["labels"][cases.watch.EXECUTION_LABEL] = "exe-stranger"
    world.api.objects["stranger"] = stranger
    assert world.reconcile(recovery) == {world.old.execution_id: "working"}


def test_every_mutation_requires_the_controller(world):
    recovery = world.recovery(world.live)
    world.grant["allowed"] = False
    for action in (
        lambda: world.reconcile(recovery),
        lambda: recovery.handle(world.old.execution_id, "oom_killed"),
    ):
        with pytest.raises(SwarmError, match="a scoped controller grant is required"):
            action()
    assert recovery.fence(world.old.execution_id) is None


def test_a_lost_authority_mid_fence_stops_before_the_next_release(world):
    recovery = world.recovery(world.live)
    require = world.live.require
    calls = []

    def flaky():
        calls.append(list(world.log))
        if world.log:
            raise SwarmError("the controller lease is stale")
        require()

    world.live.require = flaky
    with pytest.raises(SwarmError, match="stale"):
        recovery.handle(world.old.execution_id, "oom_killed")
    assert world.log == ["grant"]
    assert world.slots() == [world.old.execution_id]
    assert recovery.decision(world.old.execution_id) is None


def test_account_slot_releases_only_its_own_attempt(world):
    capacity = cases.accounts.AccountCapacity(world.client(), cases.SLUG, lambda token: None, lambda: world.clock[0])
    AccountSlot(world.client(), cases.SLUG, capacity).release(world.old.execution_id)
    assert world.slots() == []
    other = world.launch(world.live, world.fx["second"], "eng-2")
    unaccounted = replace(world.store.execution(cases.SLUG, other.execution_id), account="")
    world.store.put_agent(cases.SLUG, unaccounted)
    AccountSlot(world.client(), cases.SLUG, capacity).release(other.execution_id)
    assert world.slots() == [other.execution_id]


class _Ledger:
    def __init__(self, refusals=0, rows=None):
        self.calls, self.reads, self.refusals = [], [], refusals
        self.rows = [{"id": "task", "state": "claimed"}] if rows is None else rows

    def _refuse(self):
        if self.refusals:
            self.refusals -= 1
            raise SwarmError("ledger fixture refused")

    def state(self, slug):
        self._refuse()
        self.reads.append(slug)
        return {"tasks": [dict(row) for row in self.rows]}

    def update_task(self, slug, task_id, fields):
        self._refuse()
        self.calls.append((slug, task_id, fields, lease.EPOCH.get()))
        for row in self.rows:
            if row["id"] == task_id:
                row.update(fields)
        return {}


class _Stdout:
    def __init__(self):
        self.text, self.flushes = "", 0

    def write(self, text):
        self.text += text

    def flush(self):
        self.flushes += 1


class _Relaunch:
    def __init__(self, *statuses):
        self.statuses, self.calls, self.recovery = list(statuses), [], None

    def __call__(self, request, agent):
        self.calls.append((request, agent))
        status = self.statuses.pop(0) if self.statuses else Status.OK
        if status == Status.OK and self.recovery is not None:
            directive = self.recovery.restore(agent.execution_id)
            if directive is not None:
                self.recovery.confirm(agent.execution_id, {**directive, "command": []})
        return SimpleNamespace(outcome=Outcome("spawn", status, "kubernetes"))


def _hash(world, name):
    return world.store.redis.hgetall(world.store.key(cases.SLUG, name))


def _marks(ledger):
    return [call[2] for call in ledger.calls if "awaiting" in call[2]]


def _recorded(ledger):
    return [call[2]["recovery"] for call in ledger.calls if "recovery" in call[2]]


def _directive(world, recovery="native", checkpoint="ckpt-one-2"):
    session = world.fx["first"]["native_session"] if recovery == "native" else ""
    return {
        "checkpoint_id": checkpoint,
        "recovery": recovery,
        "session_id": session,
        "watermark": world.fx["first"]["archive_watermark"],
    }


def _outcome(world, recovery="native", checkpoint="ckpt-one-2"):
    directive = _directive(world, recovery, checkpoint)
    return {**directive, "command": [], "status": "verified", "archive_watermark": directive["watermark"]}


def test_a_pending_recovery_marks_its_task_until_the_resumed_replacement_launches_from_its_checkpoint(world):
    recovery, ledger, relaunch = world.recovery(world.live, automatic=False), _Ledger(), _Relaunch()
    relaunch.recovery = recovery
    replacements = replacement.Replacements(recovery, relaunch, ledger)
    world.show(world.old, "oom_killed")
    assert world.reconcile(recovery) == {world.old.execution_id: "fenced"}
    epoch = world.live.held.epoch

    assert replacements() == {}
    assert replacements() == {}
    assert ledger.calls == [(cases.SLUG, "task", {"awaiting": "recovery_pending"}, epoch)]
    assert _hash(world, "recovery-pending-tasks") == {"task": ""}
    assert relaunch.calls == []

    decision = recovery.decide(world.old.execution_id, "resume")
    assert replacements() == {world.old.execution_id: "ok"}
    [(request, agent)] = relaunch.calls
    successor = world.store.execution(cases.SLUG, decision.replacement)
    assert agent == successor
    assert (request.config, request.lane, request.name) == (world.store.config(cases.SLUG), "eng", successor.name)
    assert request.task == {
        "id": "task",
        "seat": successor.seat,
        "profile": successor.profile,
        "controller_epoch": epoch,
        "checkpoint": "ckpt-one-2",
    }
    assert ledger.calls[1:] == [
        (cases.SLUG, "task", {"awaiting": ""}, epoch),
        (cases.SLUG, "task", {"recovery": _outcome(world)}, epoch),
    ]
    assert _hash(world, "attempt-recovery-records") == {world.old.execution_id: "verified"}
    assert _hash(world, "recovery-pending-tasks") == {}
    assert _hash(world, "attempt-replacement-launches") == {world.old.execution_id: decision.replacement}

    assert replacements() == {}
    assert (len(relaunch.calls), len(ledger.calls)) == (1, 3)


def test_a_refused_replacement_launch_keeps_its_task_waiting_and_launches_on_a_later_pass(world):
    recovery, ledger, relaunch = world.recovery(world.live), _Ledger(), _Relaunch(Status.UNAVAILABLE)
    relaunch.recovery = recovery
    replacements = replacement.Replacements(recovery, relaunch, ledger)
    world.show(world.old, "oom_killed")
    world.reconcile(recovery)
    successor = recovery.decision(world.old.execution_id).replacement

    assert replacements() == {world.old.execution_id: "unavailable"}
    assert _marks(ledger) == [{"awaiting": "recovery_pending"}]
    assert _hash(world, "attempt-replacement-launches") == {}
    assert replacements() == {world.old.execution_id: "ok"}
    assert _marks(ledger) == [{"awaiting": "recovery_pending"}, {"awaiting": ""}]
    assert [agent.execution_id for _, agent in relaunch.calls] == [successor, successor]
    assert relaunch.calls[1][0].task["checkpoint"] == "ckpt-one-2"


def test_a_fresh_replacement_launches_without_a_checkpoint_and_a_refused_mark_waits_for_the_next_pass(
    world, monkeypatch
):
    recovery, ledger, relaunch = world.recovery(world.live, automatic=False), _Ledger(refusals=1), _Relaunch()
    relaunch.recovery = recovery
    replacements = replacement.Replacements(recovery, relaunch, ledger)
    world.show(world.old, "oom_killed")
    world.reconcile(recovery)
    stdout = _Stdout()
    monkeypatch.setattr("sys.stdout", stdout)

    assert replacements() == {}
    assert (ledger.calls, _hash(world, "recovery-pending-tasks")) == ([], {})
    assert stdout.text == "controller: the ledger refused the recovery mark on task task: ledger fixture refused\n"
    assert stdout.flushes == 1
    replacements()
    assert _marks(ledger) == [{"awaiting": "recovery_pending"}]

    decision = recovery.decide(world.old.execution_id, "fresh")
    ledger.refusals = 1
    assert replacements() == {world.old.execution_id: "deferred"}
    assert relaunch.calls == []
    assert stdout.text.endswith("controller: the ledger refused the read of task task: ledger fixture refused\n")
    assert stdout.flushes == 2
    release = ledger.update_task

    def refused_once(*args):
        ledger.update_task = release
        raise SwarmError("ledger fixture refused")

    ledger.update_task = refused_once
    reads = len(ledger.reads)
    assert replacements() == {world.old.execution_id: "ok"}
    assert ledger.reads[reads:] == [cases.SLUG]
    [(request, _)] = relaunch.calls
    successor = world.store.execution(cases.SLUG, decision.replacement)
    assert request.task == {
        "id": "task",
        "state": "claimed",
        "awaiting": "recovery_pending",
        "seat": successor.seat,
        "profile": successor.profile,
        "controller_epoch": world.live.held.epoch,
        "checkpoint": "",
    }
    assert _hash(world, "recovery-pending-tasks") == {"task": ""}
    assert _recorded(ledger) == [_outcome(world, "fresh", "")]
    assert replacements() == {}
    assert _marks(ledger) == [{"awaiting": "recovery_pending"}, {"awaiting": ""}]
    assert _hash(world, "recovery-pending-tasks") == {}


def test_a_stale_controller_records_no_launch_and_writes_no_mark(world):
    recovery, ledger, relaunch = world.recovery(world.live), _Ledger(), _Relaunch()
    relaunch.recovery = recovery
    replacements = replacement.Replacements(recovery, relaunch, ledger)
    world.show(world.old, "oom_killed")
    world.reconcile(recovery)

    def stale():
        raise SwarmError("the controller lease is stale")

    world.live.require = stale
    with pytest.raises(SwarmError, match="stale"):
        replacements()
    assert relaunch.calls == []
    assert (_hash(world, "attempt-replacement-launches"), ledger.calls) == ({}, [])


def test_a_stale_controller_writes_no_pending_mark(world):
    recovery, ledger = world.recovery(world.live, automatic=False), _Ledger()
    relaunch = _Relaunch()
    relaunch.recovery = recovery
    replacements = replacement.Replacements(recovery, relaunch, ledger)
    world.show(world.old, "oom_killed")
    world.reconcile(recovery)

    def stale():
        raise SwarmError("the controller lease is stale")

    world.live.require = stale
    with pytest.raises(SwarmError, match="stale"):
        replacements()
    assert (_hash(world, "recovery-pending-tasks"), ledger.calls) == ({}, [])


def test_a_controller_that_loses_its_lease_during_the_launch_records_no_launch(world):
    recovery, ledger = world.recovery(world.live), _Ledger()
    world.show(world.old, "oom_killed")
    world.reconcile(recovery)

    def stale():
        raise SwarmError("the controller lease is stale")

    def losing(request, agent):
        world.live.require = stale
        return SimpleNamespace(outcome=Outcome("spawn", Status.OK, "kubernetes"))

    with pytest.raises(SwarmError, match="stale"):
        replacement.Replacements(recovery, losing, ledger)()
    assert (_hash(world, "attempt-replacement-launches"), ledger.calls) == ({}, [])


def test_an_ambiguous_replacement_launch_counts_as_launched_and_is_never_placed_twice(world):
    recovery, ledger, relaunch = world.recovery(world.live), _Ledger(), _Relaunch(Status.AMBIGUOUS)
    relaunch.recovery = recovery
    replacements = replacement.Replacements(recovery, relaunch, ledger)
    world.show(world.old, "oom_killed")
    world.reconcile(recovery)
    successor = recovery.decision(world.old.execution_id).replacement

    assert replacements() == {world.old.execution_id: "ambiguous"}
    assert replacements() == {}
    assert len(relaunch.calls) == 1
    assert _hash(world, "attempt-replacement-launches") == {world.old.execution_id: successor}
    assert _marks(ledger) == [{"awaiting": "recovery_pending"}]
    assert _recorded(ledger) == [{**_directive(world), "status": "accepted"}]
    assert recovery.restore_outcomes_by_type() == {"native": 0, "reconstructed": 0, "fresh": 0}
    recovery.confirm(successor, {**_directive(world), "command": []})
    assert replacements() == {}
    assert len(relaunch.calls) == 1
    assert _marks(ledger)[-1] == {"awaiting": ""}


def test_a_replacement_the_seat_has_moved_past_is_never_launched_and_its_task_stops_waiting(world):
    recovery, ledger, relaunch = world.recovery(world.live, automatic=False), _Ledger(), _Relaunch()
    relaunch.recovery = recovery
    replacements = replacement.Replacements(recovery, relaunch, ledger)
    world.show(world.old, "oom_killed")
    world.reconcile(recovery)
    replacements()
    decision = recovery.decide(world.old.execution_id, "resume")
    successor = world.store.execution(cases.SLUG, decision.replacement)
    world.live.admit(replace(successor, **failures.NEW_ATTEMPT), successor.execution_id)

    assert replacements() == {world.old.execution_id: "superseded"}
    assert relaunch.calls == []
    assert _hash(world, "attempt-replacement-launches") == {world.old.execution_id: decision.replacement}
    assert _marks(ledger) == [{"awaiting": "recovery_pending"}, {"awaiting": ""}]
    assert _recorded(ledger) == []
    assert _hash(world, "attempt-recovery-records") == {world.old.execution_id: "superseded"}


def test_a_fence_whose_decision_is_not_written_yet_waits_without_a_launch(world):
    recovery, ledger, relaunch = world.recovery(world.live), _Ledger(), _Relaunch()
    relaunch.recovery = recovery
    replacements = replacement.Replacements(recovery, relaunch, ledger)
    fence = {"execution_id": world.old.execution_id, "reason": "oom_killed", "released": []}
    world.store.redis.hset(world.store.key(cases.SLUG, "attempt-fences"), world.old.execution_id, json.dumps(fence))

    assert replacements() == {}
    assert relaunch.calls == []
    assert _marks(ledger) == [{"awaiting": "recovery_pending"}]


@pytest.mark.parametrize(
    ("rows", "restored"),
    [
        ([{"id": "task", "state": "pr", "awaiting": "approval"}], "approval"),
        ([{"id": "task", "state": "claimed", "awaiting": "recovery_pending"}], ""),
        ([{"id": "task", "state": "claimed"}], ""),
        ([], ""),
    ],
)
def test_a_cleared_recovery_mark_restores_the_awaiting_value_the_task_held_before(world, rows, restored):
    recovery, ledger = world.recovery(world.live, automatic=False), _Ledger(rows=rows)
    relaunch = _Relaunch()
    relaunch.recovery = recovery
    replacements = replacement.Replacements(recovery, relaunch, ledger)
    world.show(world.old, "oom_killed")
    world.reconcile(recovery)

    replacements()
    assert ledger.reads == [cases.SLUG]
    assert _hash(world, "recovery-pending-tasks") == {"task": restored}
    recovery.decide(world.old.execution_id, "fresh")
    replacements()
    assert _marks(ledger) == [{"awaiting": "recovery_pending"}, {"awaiting": restored}]
    assert _hash(world, "recovery-pending-tasks") == {}


def test_the_deployed_pass_launches_a_decided_replacement_after_it_reconciles(world):
    ledger, relaunch = _Ledger(), _Relaunch()
    service = SimpleNamespace(controller=world.live, grants=_Grants(), runtime=SimpleNamespace(relaunch=relaunch))
    recover = deployed.recovery_pass(service, SimpleNamespace(image_tag="dev"), _Cluster(world), ledger)
    passing = recover.replacements
    relaunch.recovery = recover.recovery
    assert (passing.recovery, passing.relaunch, passing.ledger) == (recover.recovery, relaunch, ledger)
    assert recover() == {world.old.execution_id: "working"}
    assert ledger.calls == []
    world.ready.discard(world.fx["first"]["node"])
    _gone(world)

    assert recover() == {world.old.execution_id: "fenced"}
    assert _marks(ledger) == [{"awaiting": "recovery_pending"}]
    decision = recover.recovery.decide(world.old.execution_id, "fresh")
    assert recover() == {decision.replacement: "unobserved"}
    assert [agent.execution_id for _, agent in relaunch.calls] == [decision.replacement]
    assert _marks(ledger) == [{"awaiting": "recovery_pending"}, {"awaiting": ""}]
    kept = retention.retained(world.store, cases.SLUG, world.old.execution_id)
    assert kept["final"]["outcome_ref"] == f"attempt-fences/{world.old.execution_id}"
    assert retention.retained(world.store, cases.SLUG, decision.replacement) is None


def _context(world, checkpoint_id="ckpt-context", sequence=9):
    entry = {"checkpoint_id": checkpoint_id, "sequence": sequence, "status": "complete", "continuation": "context"}
    return {**entry, "compatibility": world.fx["compatibility"]}


def test_an_automatic_replacement_comes_only_from_a_native_checkpoint(world):
    native = {**world.fx["first"]["checkpoints"][1], "continuation": "native"}
    world.checkpoints.by_execution[world.old.execution_id] = [_context(world), native]
    recovery = world.recovery(world.live)
    decision = recovery.handle(world.old.execution_id, "node_lost")
    assert (decision.mode, decision.checkpoint) == ("resume", "ckpt-one-2")
    assert recovery.restore(decision.replacement) == _directive(world)
    assert recovery.restore_outcomes_by_type() == {"native": 0, "reconstructed": 0, "fresh": 0}


def test_a_context_only_checkpoint_waits_for_an_explicit_choice_and_restores_as_reconstructed(world):
    world.checkpoints.by_execution[world.old.execution_id] = [_context(world)]
    recovery = world.recovery(world.live)
    pending = recovery.handle(world.old.execution_id, "evicted")
    assert (pending.mode, pending.replacement) == ("recovery_pending", "")
    assert world.occupant().state == "awaiting-decision"
    assert recovery.restore_outcomes_by_type() == {"native": 0, "reconstructed": 0, "fresh": 0}
    decision = recovery.decide(world.old.execution_id, "resume")
    assert (decision.mode, decision.checkpoint) == ("resume", "ckpt-context")
    assert recovery.restore(decision.replacement) == _directive(world, "reconstructed", "ckpt-context")
    assert recovery.restore_outcomes_by_type() == {"native": 0, "reconstructed": 0, "fresh": 0}


def test_an_attempt_without_a_native_session_never_resumes_natively(world):
    run = {**world.fx["second"], "native_session": ""}
    other = world.launch(world.live, run, "eng-2")
    recovery = world.recovery(world.live)
    assert recovery.handle(other.execution_id, "oom_killed").mode == "recovery_pending"
    decision = recovery.decide(other.execution_id, "resume")
    restore = recovery.restore(decision.replacement)
    assert (restore["recovery"], restore["session_id"], restore["checkpoint_id"]) == ("reconstructed", "", "ckpt-two-1")


def test_a_fresh_choice_records_a_fresh_restore_at_the_archived_watermark(world):
    world.checkpoints.by_execution[world.old.execution_id] = []
    recovery = world.recovery(world.live)
    recovery.handle(world.old.execution_id, "node_lost")
    decision = recovery.decide(world.old.execution_id, "fresh")
    assert recovery.restore(decision.replacement) == _directive(world, "fresh", "")
    assert recovery.restore_outcomes_by_type() == {"native": 0, "reconstructed": 0, "fresh": 0}


@pytest.mark.parametrize("command", [None, 123, "claude", {"word": "claude"}, [None], [123], [False]])
def test_malformed_restore_commands_are_refused_without_recording_an_outcome(world, command):
    recovery = world.recovery(world.live)
    decision = recovery.handle(world.old.execution_id, "node_lost")
    restored = {**recovery.restore(decision.replacement), "command": command}
    with pytest.raises(SwarmError) as caught:
        recovery.confirm(decision.replacement, restored)
    assert str(caught.value) == "restore command must be a sequence of strings"
    assert recovery.outcome(decision.replacement) is None


@pytest.mark.parametrize("command", [[], ["claude", "--resume", "fixture"], ("claude", "--resume", "fixture")])
def test_restore_command_sequences_are_recorded_as_json_lists(world, command):
    recovery = world.recovery(world.live)
    decision = recovery.handle(world.old.execution_id, "node_lost")
    restored = {**recovery.restore(decision.replacement), "command": command}
    outcome = recovery.confirm(decision.replacement, restored)
    assert outcome["command"] == list(command)
    assert recovery.confirm(decision.replacement, restored) == outcome


@pytest.mark.parametrize(
    "field,value",
    [
        ("recovery", "fresh"),
        ("checkpoint_id", "another"),
        ("session_id", "another"),
        ("watermark", -1),
        ("watermark", True),
        ("watermark", 0.0),
        ("watermark", None),
    ],
)
def test_restore_receipt_fields_must_match_the_accepted_native_directive(world, field, value):
    recovery = world.recovery(world.live)
    decision = recovery.handle(world.old.execution_id, "node_lost")
    restored = {**recovery.restore(decision.replacement), "command": [], field: value}
    with pytest.raises(SwarmError) as caught:
        recovery.confirm(decision.replacement, restored)
    assert str(caught.value) == "restore outcome does not match the accepted directive"
    assert recovery.outcome(decision.replacement) is None


@pytest.mark.parametrize("restored", [None, [], {}, {"unknown": "value"}])
def test_restore_receipt_shape_is_refused_before_recording(world, restored):
    recovery = world.recovery(world.live)
    decision = recovery.handle(world.old.execution_id, "node_lost")
    with pytest.raises(SwarmError) as caught:
        recovery.confirm(decision.replacement, restored)
    assert str(caught.value) == "no restore directive for this attempt"
    assert recovery.outcome(decision.replacement) is None


def test_a_restore_receipt_requires_a_directive_and_accepts_zero_cursor(world):
    recovery = world.recovery(world.live)
    decision = recovery.handle(world.old.execution_id, "node_lost")
    restored = {**recovery.restore(decision.replacement), "command": [], "watermark": 0}
    verified = recovery.confirm(decision.replacement, restored)
    assert verified["watermark"] == 0
    with pytest.raises(SwarmError) as caught:
        recovery.confirm(decision.replacement, {**restored, "watermark": 1})
    assert str(caught.value) == "restore outcome is already recorded"
    assert recovery.outcome(decision.replacement) == verified
    world.store.redis.hdel(world.store.key(recovery.slug, "attempt-restores"), decision.replacement)
    with pytest.raises(SwarmError) as caught:
        recovery.confirm(decision.replacement, restored)
    assert str(caught.value) == "no restore directive for this attempt"


@pytest.mark.parametrize("first", ["verified", "superseded"])
def test_replacement_receipts_continue_after_an_already_settled_attempt(world, first):
    recovery, ledger, relaunch = world.recovery(world.live), _Ledger(), _Relaunch()
    relaunch.recovery = recovery
    other = world.launch(world.live, world.fx["second"], "eng-2")
    decisions = [recovery.handle(agent.execution_id, "node_lost") for agent in [world.old, other]]
    replacements = replacement.Replacements(recovery, relaunch, ledger)
    replacements()
    before, remaining = sorted(decisions, key=lambda decision: decision.execution_id)
    world.store.redis.hdel(world.store.key(recovery.slug, "attempt-recovery-records"), remaining.execution_id)
    if first == "superseded":
        successor = world.store.execution(recovery.slug, before.replacement)
        world.live.admit(replace(successor, **failures.NEW_ATTEMPT), successor.execution_id)
    ledger.calls.clear()
    replacements()
    assert _recorded(ledger) == [recovery.outcome(remaining.replacement)]
    assert len(relaunch.calls) == 2


def test_a_repeated_restore_request_reuses_the_accepted_decision_without_a_second_replacement(world):
    recovery = world.recovery(world.live, automatic=False)
    recovery.handle(world.old.execution_id, "oom_killed")
    first = recovery.request(world.old.execution_id, "resume")
    restored = recovery.restore(first.replacement)
    attempts = world.attempts()
    again = world.recovery(world.live).request(world.old.execution_id, "resume")
    assert again == first
    assert world.attempts() == attempts == [[world.old.execution_id, 1], [first.replacement, 2]]
    assert recovery.restore(first.replacement) == restored
    with pytest.raises(SwarmError, match="^no recovery decision is pending for this attempt$"):
        recovery.request(world.old.execution_id, "fresh")
    with pytest.raises(SwarmError, match="^a recovery decision is fresh or resume$"):
        recovery.request(world.old.execution_id, "recovery_pending")
    assert world.attempts() == attempts


def test_a_repeated_request_still_requires_the_controller(world):
    recovery = world.recovery(world.live, automatic=False)
    recovery.handle(world.old.execution_id, "oom_killed")
    recovery.request(world.old.execution_id, "fresh")

    def stale():
        raise SwarmError("the controller lease is stale")

    world.live.require = stale
    with pytest.raises(SwarmError, match="stale"):
        recovery.request(world.old.execution_id, "fresh")


def test_a_pending_request_is_applied_once_on_the_next_pass_and_a_bad_one_is_dropped(world, monkeypatch):
    recovery = world.recovery(world.live, automatic=False)
    recovery.handle(world.old.execution_id, "oom_killed")
    other = world.launch(world.live, world.fx["second"], "eng-2")
    requests = world.store.key(cases.SLUG, "recovery-requests")
    world.store.redis.hset(requests, mapping={world.old.execution_id: "fresh", other.execution_id: "resume"})
    stdout = _Stdout()
    monkeypatch.setattr("sys.stdout", stdout)
    world.reconcile(recovery)
    decision = recovery.decision(world.old.execution_id)
    assert (decision.mode, decision.checkpoint) == ("fresh", "")
    assert recovery.decision(other.execution_id) is None
    assert stdout.text == (
        f"controller: the recovery request for {other.execution_id} was refused: "
        "no recovery decision is pending for this attempt\n"
    )
    assert stdout.flushes == 1
    assert world.store.redis.hgetall(requests) == {}
    world.store.redis.hset(requests, world.old.execution_id, "fresh")
    attempts = world.attempts()
    world.reconcile(recovery)
    assert world.attempts() == attempts
    assert recovery.decision(world.old.execution_id) == decision


def test_an_attempt_no_recovery_admitted_has_no_restore(world):
    assert failures.restore_directive(world.store, cases.SLUG, "unknown") is None
    assert world.recovery(world.live).restore(world.old.execution_id) is None
    assert (failures.RESTORES, failures.REQUESTS) == ("attempt-restores", "recovery-requests")


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = cases.run_case(case), cases.run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text()) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True)
