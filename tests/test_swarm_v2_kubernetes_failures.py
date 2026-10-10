import json
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.swarm.store import SwarmError
from scripts.swarm_v2.kubernetes import failures
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
    with pytest.raises(SwarmError, match="only the current attempt of a seat can be fenced"):
        newer.handle(world.old.execution_id, "oom_killed")
    assert world.occupant().execution_id == replacement


def test_a_seat_moved_past_the_fenced_attempt_is_refused(world):
    recovery = world.recovery(world.live)
    recovery.handle(world.old.execution_id, "oom_killed")
    second = recovery.decision(world.old.execution_id).replacement
    third = world.live.admit(replace(world.store.execution(cases.SLUG, second), execution_id="", generation=0), second)
    assert third.generation == 3
    world.store.redis.hdel(world.store.key(cases.SLUG, "attempt-recoveries"), world.old.execution_id)
    with pytest.raises(SwarmError, match="the seat moved past the fenced attempt"):
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
