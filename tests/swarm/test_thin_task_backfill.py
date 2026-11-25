from copy import deepcopy
from typing import Any, cast

import pytest

from scripts.swarm import thin_task


def _backfill(tasks: list[dict[str, Any]], phases: set[str]) -> list[list[dict[str, Any]]]:
    module = cast(Any, thin_task)
    return cast(list[list[dict[str, Any]]], module.backfill(tasks, phases))


def _pair(name: str = "legacy") -> list[dict[str, Any]]:
    return [
        {"id": name, "title": "Fix behavior", "state": "open", "lane": "eng", "phase": "", "enhancement": "needed"},
        {"id": f"enhance-{name}", "state": "open", "lane": "plan", "kind": "plan", "phase": "", "enhances": name},
    ]


def _apply(tasks: list[dict[str, Any]], batches: list[list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], set[str]]:
    rows = {task["id"]: dict(task) for task in tasks}
    phases: set[str] = set()
    for batch in batches:
        for operation in batch:
            if operation["op"] == "phase_add":
                phases.add(operation["phase"])
            else:
                assert operation["op"] == "task_update"
                rows[operation["item"].removeprefix("tasks/")].update(operation["fields"])
    return list(rows.values()), phases


@pytest.mark.feature("thin-enhancement-phase-backfill")
def test_an_existing_enhancement_target_is_already_skipped_by_the_thin_pass() -> None:
    from scripts.tick.steps.thin import unchecked

    target, _ = _pair()
    assert unchecked(target) is False


@pytest.mark.feature("thin-enhancement-phase-backfill")
def test_legacy_pairs_get_separate_manual_phases_and_a_second_pass_writes_nothing() -> None:
    from scripts.swarm_ledger import ledger_server
    from scripts.swarm_ledger.api import schemas

    tasks = _pair() + _pair("second")
    original = deepcopy(tasks)
    batches = _backfill(tasks, set())

    assert tasks == original
    assert len(batches) == 2
    for batch, target, plan in zip(batches, tasks[::2], tasks[1::2], strict=True):
        schemas.check_operations({"operation_id": "backfill", "ops": batch, "guards": {}}, ledger_server.core, ())
        phase, moved_target, moved_plan = batch
        assert phase["op"] == "phase_add"
        assert phase["phase"] == plan["id"]
        assert phase["planning"] == "manual"
        assert moved_target["item"] == f"tasks/{target['id']}"
        assert moved_plan["item"] == f"tasks/{plan['id']}"
        assert moved_target["fields"] == moved_plan["fields"] == {"phase": plan["id"]}
    rows, phases = _apply(tasks, batches)
    assert rows == [{**task, "phase": f"enhance-{task.get('enhances', task['id'])}"} for task in original]
    assert phases == {"enhance-legacy", "enhance-second"}
    assert _backfill(rows, phases) == []


@pytest.mark.feature("thin-enhancement-phase-backfill")
def test_an_existing_own_phase_is_reused() -> None:
    tasks = _pair()
    phases = {"enhance-legacy"}
    batches = _backfill(tasks, phases)

    assert [operation["op"] for operation in batches[0]] == ["task_update", "task_update"]
    rows, _ = _apply(tasks, batches)
    assert all(task["phase"] == "enhance-legacy" for task in rows)
    assert phases == {"enhance-legacy"}


@pytest.mark.feature("thin-enhancement-phase-backfill")
def test_a_scoped_target_keeps_its_phase_and_only_its_orphan_plan_moves() -> None:
    tasks = _pair()
    tasks[0]["phase"] = "existing"
    batches = _backfill(tasks, {"existing"})

    assert len(batches) == 1
    assert len(batches[0]) == 1
    assert batches[0][0]["item"] == "tasks/enhance-legacy"
    assert batches[0][0]["fields"] == {"phase": "existing"}


@pytest.mark.feature("thin-enhancement-phase-backfill")
@pytest.mark.parametrize(
    "fields",
    [
        {"enhances": ""},
        {"state": "done"},
        {"state": "claimed"},
        {"phase": "already-scoped"},
        {"lane": "eng"},
        {"kind": "code"},
        {"done": True},
    ],
)
def test_unrelated_finished_and_scoped_plan_tasks_are_unchanged(fields: dict[str, Any]) -> None:
    tasks = _pair()
    tasks[1].update(fields)
    assert _backfill(tasks, set()) == []


@pytest.mark.feature("thin-enhancement-phase-backfill")
@pytest.mark.parametrize("state", ["done", "blocked", "pr", "claimed"])
def test_target_state_and_completion_metadata_are_preserved(state: str) -> None:
    tasks = _pair()
    tasks[0].update(state=state, done=state == "done", out_of_scope=True)
    rows, _ = _apply(tasks, _backfill(tasks, set()))
    assert rows[0] == {**tasks[0], "phase": "enhance-legacy"}


@pytest.mark.feature("thin-enhancement-phase-backfill")
def test_a_missing_target_refuses_the_pass_before_any_writes() -> None:
    _, plan = _pair()
    with pytest.raises(KeyError, match="legacy"):
        _backfill([plan], set())
