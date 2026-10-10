import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from scripts.swarm.store import SwarmError
from scripts.swarm_v2 import deployed
from scripts.swarm_v2.kubernetes import client, failures
from scripts.swarm_v2.kubernetes.failures import AccountSlot, Decision
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
    replacement = world.store.execution(cases.SLUG, world.recovery(world.live)._replace(bound))
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
    def __init__(self, world):
        self.world, self.selectors = world, []

    def list_pods(self, selector):
        self.selectors.append(selector)
        return json.loads(json.dumps(list(self.world.api.objects.values())))

    def ready_nodes(self):
        return sorted(self.world.ready)


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


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = cases.run_case(case), cases.run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text()) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True)
