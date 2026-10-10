import json
from pathlib import Path

import pytest

from scripts.swarm.store import SwarmError
from scripts.swarm_v2 import retention
from scripts.swarm_v2.kubernetes import cleanup
from scripts.swarm_v2.kubernetes.cleanup import Cleanup
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
            world.store, cases.SLUG, old.execution_id, fx["state"], fx["transcript_end"], fx["outcome_ref"]
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
    seen = []

    def require():
        seen.append(len(world.api.deletes))
        if world.api.deletes:
            raise SwarmError("the controller lease is stale")
        controller.require()

    with pytest.raises(SwarmError):
        Cleanup(world.client(), cases.SLUG, world.api, require, world.attach, world.secrets).run(old.execution_id)
    assert [kind for kind, _, _ in world.api.deletes] == ["pods"]
    assert f"swarm-attach-{old.execution_id}" in world.api.services
    assert world.log == []


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = cases.run_case(case), cases.run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text()) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True)
