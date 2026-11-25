import json
from collections.abc import Iterable
from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scripts.swarm import thin_task


def _backfill(
    tasks: list[dict[str, Any]], phases: set[str], eligible: set[str] | None = None
) -> list[list[dict[str, Any]]]:
    module = cast(Any, thin_task)
    allowed = {task["id"] for task in tasks} if eligible is None else eligible
    return cast(list[list[dict[str, Any]]], module.backfill(tasks, phases, allowed))


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


class _Client:
    def __init__(self, tasks: list[dict[str, Any]], *, refuse: bool = False) -> None:
        self.tasks = deepcopy(tasks)
        self.phases: set[str] = set()
        self.writes: list[list[dict[str, Any]]] = []
        self.reads: list[tuple[str, ...]] = []
        self.refuse = refuse
        self.held: set[str] = set()
        self.pauses: list[float] = []

    def tasks_where(
        self,
        slug: str,
        *,
        ids: Iterable[str] = (),
        states: Iterable[str] = (),
        fields: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        assert slug == "proof"
        wanted = tuple(ids)
        self.reads.append(wanted)
        assert wanted or tuple(states) == ("open",)
        return [
            {key: value for key, value in task.items() if key in fields}
            for task in self.tasks
            if (not wanted or task["id"] in wanted) and (not states or task["state"] in states)
        ]

    def focus_verdicts(self, slug: str, target: str) -> dict[str, Any]:
        assert (slug, target) == ("proof", "lane:plan")
        return {
            "tasks": [
                {"id": task["id"], "verdict": "held" if task["id"] in self.held else "claimable"}
                for task in self.tasks
                if task["lane"] == "plan" and task["state"] == "open"
            ]
        }

    def collection(self, slug: str, name: str) -> list[dict[str, Any]]:
        assert (slug, name) == ("proof", "phases")
        return [{"id": phase} for phase in sorted(self.phases)]

    def mutate(self, slug: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
        assert slug == "proof"
        self.writes.append(operations)
        if self.refuse:
            return {"rejected": ["refused"], "_meta": {"warnings": ["repair refused"]}}
        self.tasks, added = _apply(self.tasks, [operations])
        self.phases.update(added)
        return {}


def _command(
    monkeypatch: pytest.MonkeyPatch,
    client: _Client,
    extra: tuple[str, ...] = (),
    duration: float = 0.125,
) -> int:
    from scripts.swarm import ledger_client
    from scripts.tick.adapters import ledger

    monkeypatch.setattr(ledger_client, "LedgerClient", lambda: client)
    monkeypatch.setattr(ledger, "resource_client", lambda slug: lambda remaining: client)
    ticks = iter(index * duration for index in range(1000))
    clock = SimpleNamespace(monotonic=lambda: next(ticks), sleep=client.pauses.append)
    monkeypatch.setattr(thin_task, "time", clock, raising=False)
    module = cast(Any, thin_task)
    return cast(int, module.main(["--slug", "proof", *extra]))


@pytest.mark.feature("thin-enhancement-phase-backfill")
def test_the_command_reads_bounded_targets_and_reports_verified_before_and_after_counts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = _Client([task for index in range(101) for task in _pair(f"legacy-{index}")])

    assert _command(monkeypatch, client) == 0
    assert json.loads(capsys.readouterr().out) == {
        "before": 101,
        "moved": 101,
        "after": 0,
        "batch_seconds": [0.125] * 5,
    }
    assert [len(ids) for ids in client.reads if ids] == [100, 1, 100, 1, 100, 1]
    assert len(client.writes) == 5
    assert [len(batch) for batch in client.writes] == [75, 75, 75, 75, 3]
    assert client.pauses == [1.0] * 4

    assert _command(monkeypatch, client) == 0
    assert json.loads(capsys.readouterr().out) == {"before": 0, "moved": 0, "after": 0, "batch_seconds": []}
    assert len(client.writes) == 5


@pytest.mark.feature("thin-enhancement-phase-backfill")
def test_a_refused_api_write_is_visible_and_never_reports_zero_remaining(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.swarm.ledger_client import LedgerRefused

    client = _Client(_pair(), refuse=True)
    with pytest.raises(LedgerRefused, match="repair refused"):
        _command(monkeypatch, client)
    assert capsys.readouterr().out == ""
    assert all(task["phase"] == "" for task in client.tasks)


@pytest.mark.feature("thin-enhancement-phase-backfill")
def test_the_command_fails_if_readback_still_finds_an_unphased_plan(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = _Client(_pair())
    monkeypatch.setattr(client, "mutate", lambda slug, operations: {})
    assert _command(monkeypatch, client) == 1
    assert json.loads(capsys.readouterr().out) == {"before": 1, "moved": 1, "after": 1, "batch_seconds": [0.125]}


@pytest.mark.feature("thin-enhancement-phase-backfill")
def test_a_plan_missing_from_the_claimable_set_is_unchanged() -> None:
    tasks = _pair()
    assert _backfill(tasks, set(), set()) == []


@pytest.mark.feature("thin-enhancement-phase-backfill")
def test_the_command_leaves_frozen_and_held_pairs_untouched(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tasks = _pair() + _pair("frozen") + _pair("held")
    client = _Client(tasks)
    client.held = {"enhance-frozen", "enhance-held"}

    assert _command(monkeypatch, client) == 0
    assert json.loads(capsys.readouterr().out) == {"before": 1, "moved": 1, "after": 0, "batch_seconds": [0.125]}
    assert client.tasks[2:] == tasks[2:]
    assert len(client.writes) == 1


@pytest.mark.feature("thin-enhancement-phase-backfill")
def test_preview_reports_the_eligible_count_without_writes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = _Client(_pair())
    assert _command(monkeypatch, client, ("--dry-run",)) == 0
    assert json.loads(capsys.readouterr().out) == {"before": 1, "moved": 0, "after": 1, "batch_seconds": []}
    assert client.writes == []
    assert client.pauses == []


@pytest.mark.feature("thin-enhancement-phase-backfill")
@pytest.mark.parametrize("duration, moved, remaining", [(2.0, 26, 0), (2.5, 25, 1)])
def test_a_write_over_two_seconds_stops_the_pass_before_another_batch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    duration: float,
    moved: int,
    remaining: int,
) -> None:
    client = _Client([task for index in range(26) for task in _pair(f"legacy-{index}")])
    assert _command(monkeypatch, client, duration=duration) == int(bool(remaining))
    writes = 1 if remaining else 2
    assert json.loads(capsys.readouterr().out) == {
        "before": 26,
        "moved": moved,
        "after": remaining,
        "batch_seconds": [duration] * writes,
    }
    assert len(client.writes) == writes
    assert client.pauses == [1.0] * (writes - 1)
