import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from scripts.swarm.store import AgentRecord, SwarmError
from scripts.swarm_v2 import retention
from scripts.swarm_v2.kubernetes import cleanup, failures
from scripts.swarm_v2.kubernetes.cleanup import Cleanup
from scripts.swarm_v2.kubernetes.client import ApiRefused
from scripts.swarm_v2.kubernetes.completion import Completion
from scripts.swarm_v2.kubernetes.watch import BACKEND
from tests import sv2_kub04_cases as cases

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-KUB-04"


@pytest.fixture
def ready():
    fx = cases.fixture("first")
    world = cases.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        old, replacement = world.attempts(controller, fx)
        retention.finalize(
            world.store,
            cases.SLUG,
            controller.require,
            old.execution_id,
            fx["state"],
            fx["transcript_end"],
            fx["outcome_ref"],
        )
        world.acknowledge(old.execution_id, fx["transcript_end"])
        yield world, controller, old, replacement


def test_steps_run_pods_then_services_then_attach_then_secrets():
    assert cleanup.KINDS == ("pods", "services")
    assert cleanup.STEPS == ("pods", "services", "attach", "secrets")


def test_pins_only_the_old_attempt_by_owner_execution_and_generation(ready):
    world, controller, old, _ = ready
    world.api.drop_next_delete = True
    with pytest.raises(TimeoutError):
        world.cleanup(controller).run(old.execution_id)
    journal = json.loads(world.store.redis.hget(world.store.key(cases.SLUG, "cleanup-journal"), old.execution_id))
    assert journal == {
        "execution_id": old.execution_id,
        "generation": 1,
        "pinned": {
            "pods": [{"name": f"swarm-{old.execution_id}", "uid": "uid-1"}],
            "services": [{"name": f"swarm-attach-{old.execution_id}", "uid": "uid-2"}],
        },
        "sent": ["uid-1"],
        "done": [],
        "removed": {"pods": [], "services": []},
    }


def test_a_pod_of_another_generation_or_owner_is_never_pinned(ready):
    world, controller, old, _ = ready
    pod = world.api.objects[f"swarm-{old.execution_id}"]
    pod["metadata"]["labels"][cases.GENERATION_LABEL] = "2"
    service = world.api.services[f"swarm-attach-{old.execution_id}"]
    service["metadata"]["labels"][cases.watch.OWNER_LABEL] = "agentihooks-swarm-other"
    result = world.cleanup(controller).run(old.execution_id)
    assert result.state == "done"
    assert result.removed == {"pods": [], "services": []}
    assert world.api.deletes == []
    assert f"swarm-{old.execution_id}" in world.api.objects


def test_done_cleanup_returns_the_retained_removal_without_calls(ready):
    world, controller, old, _ = ready
    first = world.cleanup(controller).run(old.execution_id)
    calls, releases = list(world.api.deletes), list(world.log)
    again = world.cleanup(controller).run(old.execution_id)
    assert again == first
    assert world.api.deletes == calls
    assert world.log == releases


def test_suspended_cleanup_reads_nothing_and_needs_no_controller(ready):
    world, _, old, _ = ready

    def refuse():
        raise AssertionError("suspended cleanup must not check authority")

    suspended = Cleanup(world.client(), cases.SLUG, world.api, refuse, world.attach, world.secrets, enabled=False)
    assert suspended.run(old.execution_id) == cleanup.Result(old.execution_id, "suspended")
    assert world.api.deletes == []


def test_authority_is_checked_before_every_delete_and_release(ready):
    world, controller, old, _ = ready
    calls = []

    def require():
        calls.append((len(world.api.deletes), len(world.log)))
        controller.require()

    Cleanup(world.client(), cases.SLUG, world.api, require, world.attach, world.secrets).run(old.execution_id)
    assert (0, 0) in calls
    assert (1, 0) in calls
    assert (2, 0) in calls
    assert (2, 1) in calls


def test_a_lost_authority_mid_run_stops_before_the_next_delete(ready):
    world, controller, old, _ = ready

    def require():
        if world.api.deletes:
            raise SwarmError("the controller lease is stale")
        controller.require()

    with pytest.raises(SwarmError):
        Cleanup(world.client(), cases.SLUG, world.api, require, world.attach, world.secrets).run(old.execution_id)
    assert [kind for kind, _, _ in world.api.deletes] == ["pods"]
    assert f"swarm-attach-{old.execution_id}" in world.api.services
    assert world.log == []
    journal = json.loads(world.store.redis.hget(world.store.key(cases.SLUG, "cleanup-journal"), old.execution_id))
    assert journal["sent"] == ["uid-1"]
    assert journal["done"] == []
    assert journal["removed"] == {"pods": [], "services": []}


def _journal(world, old, removed: list) -> None:
    journal = {
        "execution_id": old.execution_id,
        "generation": old.generation,
        "pinned": {
            "pods": [
                {"name": f"swarm-{old.execution_id}", "uid": "uid-1"},
                {"name": "swarm-extra", "uid": "uid-5"},
            ],
            "services": [],
        },
        "sent": [],
        "done": [],
        "removed": {"pods": removed, "services": []},
    }
    world.store.redis.hset(world.store.key(cases.SLUG, "cleanup-journal"), old.execution_id, json.dumps(journal))


def test_a_resumed_step_skips_settled_objects_without_a_second_record(ready):
    world, controller, old, _ = ready
    extra = json.loads(json.dumps(world.api.objects[f"swarm-{old.execution_id}"]))
    extra["metadata"]["name"] = "swarm-extra"
    assert world.api.put(extra)["metadata"]["uid"] == "uid-5"
    settled = {"name": f"swarm-{old.execution_id}", "uid": "uid-1", "outcome": "deleted"}
    _journal(world, old, [settled])
    result = world.cleanup(controller).run(old.execution_id)
    assert result.removed["pods"] == [settled, {"name": "swarm-extra", "uid": "uid-5", "outcome": "deleted"}]
    assert world.api.deletes == [("pods", "swarm-extra", "uid-5")]
    assert f"swarm-{old.execution_id}" in world.api.objects


def test_a_pinned_object_no_longer_selected_is_never_deleted(ready):
    world, controller, old, _ = ready
    pod = world.api.objects[f"swarm-{old.execution_id}"]
    pod["metadata"]["labels"][cases.watch.OWNER_LABEL] = "agentihooks-swarm-other"
    _journal(world, old, [])
    result = world.cleanup(controller).run(old.execution_id)
    assert result.removed["pods"] == [
        {"name": f"swarm-{old.execution_id}", "uid": "uid-1", "outcome": "unselected"},
        {"name": "swarm-extra", "uid": "uid-5", "outcome": "absent"},
    ]
    assert [call for call in world.api.deletes if call[0] == "pods"] == []
    assert f"swarm-{old.execution_id}" in world.api.objects


def test_a_pinned_object_stripped_of_every_label_is_never_deleted(ready):
    world, controller, old, _ = ready
    del world.api.objects[f"swarm-{old.execution_id}"]["metadata"]["labels"]
    _journal(world, old, [])
    result = world.cleanup(controller).run(old.execution_id)
    assert result.removed["pods"] == [
        {"name": f"swarm-{old.execution_id}", "uid": "uid-1", "outcome": "unselected"},
        {"name": "swarm-extra", "uid": "uid-5", "outcome": "absent"},
    ]
    assert [call for call in world.api.deletes if call[0] == "pods"] == []
    assert f"swarm-{old.execution_id}" in world.api.objects


def test_not_final_and_waiting_results_name_their_execution(ready):
    world, controller, _, replacement = ready
    run = world.cleanup(controller).run
    assert run(replacement.execution_id) == cleanup.Result(replacement.execution_id, "not_final")
    retention.finalize(
        world.store, cases.SLUG, controller.require, replacement.execution_id, "completed", 3, "ledger:r"
    )
    assert run(replacement.execution_id) == cleanup.Result(replacement.execution_id, "waiting_archive")


def test_a_name_taken_over_between_listing_and_delete_is_recorded_replaced(ready, monkeypatch):
    world, controller, old, _ = ready
    name = f"swarm-{old.execution_id}"
    delete = world.api.delete

    def swap_then_delete(kind, target, uid):
        if kind == "pods":
            replacement = json.loads(json.dumps(world.api.objects[name]))
            world.api.objects.pop(name)
            world.api.put(replacement)
        return delete(kind, target, uid)

    monkeypatch.setattr(world.api, "delete", swap_then_delete)
    result = world.cleanup(controller).run(old.execution_id)
    assert result.removed["pods"] == [{"name": name, "uid": "uid-1", "outcome": "replaced"}]
    assert world.api.objects[name]["metadata"]["uid"] == "uid-5"


def test_a_journal_left_after_retain_is_cleared_on_the_next_run(ready):
    world, controller, old, _ = ready
    first = world.cleanup(controller).run(old.execution_id)
    key = world.store.key(cases.SLUG, "cleanup-journal")
    world.store.redis.hset(key, old.execution_id, "{}")
    assert world.cleanup(controller).run(old.execution_id) == first
    assert world.store.redis.hlen(key) == 0


def _finished_and_running(world, controller):
    fx = cases.fixture("first")
    finished, running = world.admit(controller, "eng-1"), world.admit(controller, "eng-2")
    for attempt in (finished, running):
        world.spawn(controller, attempt, fx)
    world.api.objects[f"swarm-{finished.execution_id}"]["status"]["phase"] = "Succeeded"
    world.api.objects[f"swarm-{running.execution_id}"]["status"]["phase"] = "Running"
    return finished, running


def _listed(world) -> list[dict]:
    return json.loads(json.dumps(list(world.api.objects.values())))


def test_a_completed_attempt_pod_is_deleted_and_a_running_one_kept():
    world = cases.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        finished, running = _finished_and_running(world, controller)
        world.acknowledge(finished.execution_id, 7)
        complete = Completion(world.client(), cases.SLUG, world.cleanup(controller))
        assert complete(_listed(world)) == {finished.execution_id: "done"}
        assert sorted(world.api.objects) == [f"swarm-{running.execution_id}"]
        assert sorted(world.api.services) == [f"swarm-attach-{running.execution_id}"]
        final = retention.final(world.store, cases.SLUG, finished.execution_id)
        pod = f"pods/swarm-{finished.execution_id}/uid-1"
        assert (final.state, final.transcript_end, final.outcome_ref) == ("completed", 7, pod)
        assert retention.final(world.store, cases.SLUG, running.execution_id) is None
        assert world.attached == world.secret_refs == {running.execution_id}
        assert complete(_listed(world)) == {}


@pytest.mark.parametrize("change", ["generation", "fenced", "duplicate"])
def test_a_succeeded_pod_of_another_generation_a_fenced_attempt_or_a_second_pod_is_left_alone(change):
    world = cases.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        finished, _ = _finished_and_running(world, controller)
        pod = world.api.objects[f"swarm-{finished.execution_id}"]
        if change == "generation":
            pod["metadata"]["labels"][cases.GENERATION_LABEL] = "2"
        elif change == "fenced":
            world.store.redis.hset(world.store.key(cases.SLUG, failures.FENCES), finished.execution_id, "{}")
        else:
            world.api.put({**json.loads(json.dumps(pod)), "metadata": {**pod["metadata"], "name": "swarm-twin"}})
        complete = Completion(world.client(), cases.SLUG, world.cleanup(controller))
        assert complete(_listed(world)) == {}
        assert f"swarm-{finished.execution_id}" in world.api.objects
        assert world.api.deletes == []
        assert retention.final(world.store, cases.SLUG, finished.execution_id) is None


def test_a_superseded_attempt_whose_pod_succeeded_is_deleted_too():
    world = cases.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        finished, _ = _finished_and_running(world, controller)
        target = {"pod_namespace": world.api.namespace, "pod_name": "swarm-pending"}
        name = world.store.next_name(cases.SLUG, "eng")
        successor = AgentRecord(name, "eng", "task", seat=finished.seat, runtime_backend=BACKEND)
        controller.admit(replace(successor, runtime_target=target), finished.execution_id)
        complete = Completion(world.client(), cases.SLUG, world.cleanup(controller))
        assert complete(_listed(world)) == {finished.execution_id: "done"}
        assert f"swarm-{finished.execution_id}" not in world.api.objects


def test_a_refused_delete_keeps_the_pod_and_retries_on_the_next_pass(monkeypatch, capsys):
    world = cases.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        finished, _ = _finished_and_running(world, controller)
        complete = Completion(world.client(), cases.SLUG, world.cleanup(controller))
        delete = world.api.delete

        def refuse(kind, name, uid):
            raise ApiRefused(500, "internal error")

        monkeypatch.setattr(world.api, "delete", refuse)
        assert complete(_listed(world)) == {finished.execution_id: "refused"}
        line = f"controller: cleanup of {finished.execution_id} failed with ApiRefused; retrying\n"
        assert capsys.readouterr().out == line
        assert f"swarm-{finished.execution_id}" in world.api.objects
        monkeypatch.setattr(world.api, "delete", delete)
        assert complete(_listed(world)) == {finished.execution_id: "done"}
        assert f"swarm-{finished.execution_id}" not in world.api.objects


def _fence(world, attempt, mode):
    world.api.objects[f"swarm-{attempt.execution_id}"]["status"]["phase"] = "Failed"
    world.acknowledge(attempt.execution_id, 5)
    fence = {"execution_id": attempt.execution_id, "generation": attempt.generation, "archive_watermark": 5}
    decision = failures.Decision(attempt.execution_id, attempt.generation, failures.APPLICATION_EXIT, mode)
    for name, value in ((failures.FENCES, fence), (failures.RECOVERIES, asdict(decision))):
        world.store.redis.hset(world.store.key(cases.SLUG, name), attempt.execution_id, json.dumps(value))


def _failed_and_fenced(world, controller, mode):
    failed, running = _finished_and_running(world, controller)
    _fence(world, failed, mode)
    return failed, running


@pytest.mark.parametrize("mode", [failures.FRESH, failures.RESUME])
def test_a_failed_attempt_whose_recovery_is_decided_is_finalized_and_its_pod_deleted(mode):
    world = cases.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        failed, running = _failed_and_fenced(world, controller, mode)
        complete = Completion(world.client(), cases.SLUG, world.cleanup(controller))
        assert complete(_listed(world)) == {failed.execution_id: "done"}
        assert sorted(world.api.objects) == [f"swarm-{running.execution_id}"]
        assert sorted(world.api.services) == [f"swarm-attach-{running.execution_id}"]
        outcome = f"attempt-fences/{failed.execution_id}"
        expected = retention.Final(failed.execution_id, failed.generation, "cancelled", 5, outcome)
        assert retention.final(world.store, cases.SLUG, failed.execution_id) == expected
        assert retention.final(world.store, cases.SLUG, running.execution_id) is None
        assert world.attached == world.secret_refs == {running.execution_id}
        assert complete(_listed(world)) == {}


def test_a_pending_attempt_listed_first_does_not_stop_a_decided_one_after_it():
    world = cases.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        first, second = sorted(_finished_and_running(world, controller), key=lambda attempt: attempt.execution_id)
        _fence(world, first, failures.PENDING)
        _fence(world, second, failures.FRESH)
        complete = Completion(world.client(), cases.SLUG, world.cleanup(controller))
        assert complete(_listed(world)) == {second.execution_id: "done"}
        assert sorted(world.api.objects) == [f"swarm-{first.execution_id}"]
        assert retention.final(world.store, cases.SLUG, first.execution_id) is None
        for name in (failures.FENCES, failures.RECOVERIES):
            assert world.store.redis.hexists(world.store.key(cases.SLUG, name), second.execution_id)


def test_a_failed_attempt_awaiting_its_recovery_decision_keeps_its_pod():
    world = cases.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        failed, _ = _failed_and_fenced(world, controller, failures.PENDING)
        complete = Completion(world.client(), cases.SLUG, world.cleanup(controller))
        assert complete(_listed(world)) == {}
        assert f"swarm-{failed.execution_id}" in world.api.objects
        assert world.api.deletes == []
        assert retention.final(world.store, cases.SLUG, failed.execution_id) is None


def test_a_replaced_attempt_ends_at_its_fenced_watermark_though_a_later_heartbeat_moved_on():
    world = cases.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        failed, _ = _failed_and_fenced(world, controller, failures.FRESH)
        world.acknowledge(failed.execution_id, 9)
        complete = Completion(world.client(), cases.SLUG, world.cleanup(controller))
        assert complete(_listed(world)) == {failed.execution_id: "done"}
        assert retention.final(world.store, cases.SLUG, failed.execution_id).transcript_end == 5
        assert f"swarm-{failed.execution_id}" not in world.api.objects


def test_a_replaced_attempt_keeps_its_pod_while_the_archive_is_behind_its_fenced_watermark():
    world = cases.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        failed, _ = _failed_and_fenced(world, controller, failures.FRESH)
        world.acknowledge(failed.execution_id, 4)
        complete = Completion(world.client(), cases.SLUG, world.cleanup(controller))
        assert complete(_listed(world)) == {failed.execution_id: "waiting_archive"}
        assert f"swarm-{failed.execution_id}" in world.api.objects
        assert world.api.deletes == []
        world.acknowledge(failed.execution_id, 5)
        assert complete(_listed(world)) == {failed.execution_id: "done"}
        assert f"swarm-{failed.execution_id}" not in world.api.objects


def test_an_attempt_already_final_under_another_outcome_is_cleaned_without_a_second_final():
    world = cases.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        failed, _ = _failed_and_fenced(world, controller, failures.FRESH)
        removed = f"proof/{cases.SLUG}/removed"
        retention.finalize(world.store, cases.SLUG, controller.require, failed.execution_id, "cancelled", 5, removed)
        complete = Completion(world.client(), cases.SLUG, world.cleanup(controller))
        assert complete(_listed(world)) == {failed.execution_id: "done"}
        assert retention.final(world.store, cases.SLUG, failed.execution_id).outcome_ref == removed
        assert f"swarm-{failed.execution_id}" not in world.api.objects


def test_a_replaced_attempt_is_not_finalized_without_the_controller_lease():
    world = cases.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        failed, _ = _failed_and_fenced(world, controller, failures.FRESH)

        def stale():
            raise SwarmError("the controller lease is stale")

        cleanup = Cleanup(world.client(), cases.SLUG, world.api, stale, world.attach, world.secrets)
        with pytest.raises(SwarmError):
            Completion(world.client(), cases.SLUG, cleanup)(_listed(world))
        assert retention.final(world.store, cases.SLUG, failed.execution_id) is None
        assert f"swarm-{failed.execution_id}" in world.api.objects


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = cases.run_case(case), cases.run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text()) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True)
