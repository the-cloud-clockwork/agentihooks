from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from scripts.swarm_v2 import deployed, retention
from scripts.swarm_v2.kubernetes import pool, unschedulable
from scripts.swarm_v2.kubernetes.failures import FENCES
from tests import sv2_kub05_cases as cases
from tests.test_swarm_v2_kubernetes_failures import _Cleaning, _Grants, _Ledger, _unschedulable
from tests.test_swarm_v2_kubernetes_failures import world as world

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "condition",
    [
        {"type": "PodScheduled", "status": "False", "reason": "SchedulingGated"},
        {"type": "PodScheduled", "status": "True", "reason": "Unschedulable"},
        {"type": "Ready", "status": "False", "reason": "Unschedulable"},
    ],
)
def test_only_the_unschedulable_scheduling_condition_releases_capacity(condition: dict[str, str]) -> None:
    pod = {"spec": {"containers": []}, "status": {"phase": "Pending", "conditions": [condition]}}

    assert not pool.unschedulable(pod)
    assert pool.usage([pod]).pods == 1


@pytest.mark.parametrize("claimant", ["", "owned"])
def test_reopening_preserves_the_ledger_slug_owner_and_controller_epoch(world: Any, claimant: str) -> None:
    owner = world.old.name if claimant == "owned" else ""
    ledger = _Ledger(rows=[{"id": "task", "state": "claimed", "claimed_by": owner}])

    assert unschedulable.reopen(world.store, cases.SLUG, world.live, ledger, world.old)
    assert ledger.reads == [cases.SLUG]
    assert ledger.calls == [(cases.SLUG, "task", unschedulable.REOPENED, world.live.held.epoch)]


@pytest.mark.parametrize("field,value", [("state", "pr"), ("claimed_by", "new-worker")])
def test_a_claim_that_moves_during_reopening_is_preserved(
    world: Any, monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    ledger = _Ledger(rows=[{"id": "task", "state": "claimed", "claimed_by": world.old.name}])
    update = ledger.update_task

    def racing(
        slug: str,
        task_id: str,
        fields: dict[str, Any],
        *,
        if_state: tuple[str, ...] = (),
        if_match: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ledger.rows[0][field] = value
        return update(slug, task_id, fields, if_state=if_state, if_match=if_match)

    monkeypatch.setattr(ledger, "update_task", racing)

    assert not unschedulable.reopen(world.store, cases.SLUG, world.live, ledger, world.old)
    assert ledger.rows[0][field] == value
    assert ledger.calls == []
    assert world.store.execution(cases.SLUG, world.old.execution_id).state != "ended"


def test_an_execution_whose_task_was_removed_can_end_without_reopening_work(world: Any) -> None:
    ledger = _Ledger(rows=[])

    assert unschedulable.reopen(world.store, cases.SLUG, world.live, ledger, world.old)
    assert ledger.calls == []
    assert world.store.execution(cases.SLUG, world.old.execution_id).state == "ended"


def test_a_refused_reopening_reports_the_task_and_flushes_the_diagnostic(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = Mock()
    monkeypatch.setattr("builtins.print", output)

    assert not unschedulable.reopen(world.store, cases.SLUG, world.live, _Ledger(refusals=1), world.old)
    output.assert_called_once_with(
        "controller: the ledger refused to reopen task task: ledger fixture refused", flush=True
    )


def test_unlabelled_unschedulable_pods_never_end_a_worker(world: Any) -> None:
    _unschedulable(world, world.old)
    pod = world.api.objects[f"swarm-{world.old.execution_id}"]
    pod["metadata"].pop("labels")
    ledger = _Ledger()
    ending = unschedulable.Unschedulable(world.store, cases.SLUG, world.live, ledger, window_ms=1)

    assert ending([pod]) == {}
    assert ledger.calls == []


@pytest.mark.parametrize("blocked", ["fenced", "foreign", "final"])
def test_ineligible_attempts_never_prevent_a_later_unschedulable_execution_from_ending(
    world: Any, monkeypatch: pytest.MonkeyPatch, blocked: str
) -> None:
    second = world.launch(world.live, world.fx["first"], seat="eng-2")
    _unschedulable(world, world.old)
    _unschedulable(world, second)
    first = world.store.execution(cases.SLUG, world.old.execution_id)
    if blocked == "fenced":
        world.store.redis.hset(world.store.key(cases.SLUG, FENCES), first.execution_id, "fenced")
    elif blocked == "foreign":
        first = replace(first, runtime_backend="herdr")
    else:
        retention.finalize(world.store, cases.SLUG, world.live.require, first.execution_id, "cancelled", 0)
    monkeypatch.setattr(world.store.execution_registry, "records", lambda slug: [first, second])
    ledger = _Ledger()
    ending = unschedulable.Unschedulable(world.store, cases.SLUG, world.live, ledger, window_ms=1)
    pods = list(world.api.objects.values())

    assert ending(pods) == {}
    key = world.store.key(cases.SLUG, unschedulable.SINCE)
    assert world.store.redis.hgetall(key) == {second.execution_id: str(world.clock[0])}
    world.clock[0] += 1
    assert ending(pods) == {second.execution_id: "ended"}


def test_the_deployed_pass_cancels_and_deletes_an_unschedulable_execution(world: Any) -> None:
    ledger = _Ledger(rows=[{"id": "task", "state": "claimed", "claimed_by": world.old.name}])
    recover = deployed.recovery_pass(
        cast(Any, SimpleNamespace(controller=world.live, grants=_Grants(), runtime=SimpleNamespace(relaunch=None))),
        cast(Any, SimpleNamespace(image_tag="dev")),
        cast(Any, _Cleaning(world)),
        ledger,
    )
    assert recover.ending is not None
    recover.ending.window_ms = 1
    _unschedulable(world, world.old)

    recover()
    world.clock[0] += 1
    recover()

    assert world.store.execution(cases.SLUG, world.old.execution_id).state == "ended"
    assert ledger.rows == [{"id": "task", **unschedulable.REOPENED}]
    assert f"swarm-{world.old.execution_id}" not in world.api.objects
