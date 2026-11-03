"""The task steps run the tick's hand written task passes, settle and close space at their sites in today's order."""

from types import SimpleNamespace
from typing import Any

import pytest

import scripts.swarm.cli  # noqa: F401
from scripts.swarm import (
    claim_hold,
    dev_red,
    difficulty,
    fleet_red,
    grouping,
    prerequisite_wait,
    promotion_watch,
    release_wait,
)
from scripts.tick import engine, registry
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain
from scripts.tick.steps import PASS, CallStep, actions, budgets, tasks

pytestmark = pytest.mark.unit


class _State:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state

    def read(self, domain: Domain, key: str) -> object | None:
        return self.state.get(key) if domain == PASS else None


def _pass(box: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "slug": "sw",
        "config": "config",
        "store": "store",
        "ledger": "ledger",
        "runtime": "runtime",
        "rows": "rows",
        "doc": "doc",
        "now_ms": 7,
        "box": {} if box is None else box,
    }


def _ctx(state: dict[str, Any]) -> Context:
    return Context(_State(state), Deadline.after(60.0))


def _tick(calls: list[tuple[Any, ...]], raises: object = None) -> SimpleNamespace:
    def skip_refused(function: Any, *args: Any) -> list[str]:
        calls.append((function, *args))
        if function is raises:
            raise RuntimeError("boom")
        return [f"ran {len(calls)}"]

    return SimpleNamespace(
        skip_refused=skip_refused,
        run_window=SimpleNamespace(step="run window"),
        dispatcher=SimpleNamespace(group="dispatcher group"),
        claim_hold=claim_hold,
        difficulty=difficulty,
        grouping=grouping,
        _settle="settle",
        _close_space="close space",
    )


def _run(group: str, tick: SimpleNamespace, state: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setattr("scripts.tick.steps.import_module", lambda name: tick)
    return actions(engine.run_group(group, _ctx(state)))


def test_the_task_steps_sit_at_their_sites_in_legacy_order() -> None:
    assert tasks.TASK_PASSES == "task passes"
    assert tasks.CLOSE == "close"
    assert [step.name for step in engine.group_steps(tasks.TASK_PASSES)] == [
        "run window",
        "verification notices",
        "size pass",
        "grouping release",
        "dispatcher group",
    ]
    assert [step.name for step in engine.group_steps(tasks.CLOSE)][-2:] == ["settle", "close space"]
    assert [step.group for step in tasks.STEPS] == (
        [tasks.DRAINED] * 2 + [tasks.REOPENS] * 5 + [tasks.TASK_PASSES] * 5 + [tasks.CLOSE] * 2
    )
    registered = [step for wave in registry.waves() for step in wave]
    for step in tasks.STEPS:
        assert step in registered
        assert (step.lane, step.budget_s) == (CallStep.lane, budgets.timing_of(step.name)[0])


def test_the_task_passes_run_their_calls_with_the_pass_state(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    found = _run(tasks.TASK_PASSES, _tick(calls), _pass(), monkeypatch)
    assert found == ["ran 1", "ran 2", "ran 3", "ran 4", "ran 5"]
    assert calls == [
        ("run window", "sw", "config", "store", "ledger", "doc", 7),
        (claim_hold.verification_notices, "sw", "store", "doc", 7),
        (difficulty.size_pass, "sw", "ledger", "doc"),
        (grouping.release_pass, "sw", "store", "ledger", "doc"),
        ("dispatcher group", "sw", "config", "store", "ledger", "doc", 7),
    ]


def test_the_reopen_passes_run_their_calls_with_the_pass_state(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    found = _run(tasks.REOPENS, _tick(calls), _pass(), monkeypatch)
    assert found == ["ran 1", "ran 2", "ran 3", "ran 4", "ran 5"]
    assert calls == [
        (dev_red.reopen_pass, "sw", "config", "store", "ledger", "rows"),
        (prerequisite_wait.reopen_pass, "sw", "store", "ledger", "rows"),
        (promotion_watch.refresh, "sw", "config", "store", "ledger", "doc", 7),
        (release_wait.alert_pass, "sw", "config", "ledger", "doc"),
        (fleet_red.notice_pass, "sw", "store", "ledger", "rows", 7),
    ]


@pytest.mark.parametrize(
    ("sleeping", "state", "claimable", "expected", "asked"),
    [
        (False, "drained", ("ci",), ["new tasks, running again"], ["eng", "ci"]),
        (False, "drained", ("eng",), ["new tasks, running again"], ["eng"]),
        (True, "drained", ("eng", "ci"), [], []),
        (False, "running", ("eng", "ci"), [], []),
        (False, "drained", (), [], ["eng", "ci"]),
    ],
)
def test_a_drained_swarm_with_claimable_work_runs_again(
    monkeypatch: pytest.MonkeyPatch,
    sleeping: bool,
    state: str,
    claimable: tuple[str, ...],
    expected: list[str],
    asked: list[str],
) -> None:
    updates: list[tuple[str, str]] = []
    seen: list[tuple[Any, ...]] = []
    store = SimpleNamespace(update=lambda slug, state: updates.append((slug, state)) or "resumed")

    def claimable_in(slug: str, store_seen: Any, rows: Any, doc: Any, lane: str) -> bool:
        seen.append((slug, store_seen, rows, doc, lane))
        return lane in claimable

    def sleeping_of(slug: str, store_seen: Any, rows: Any) -> bool:
        seen.append((slug, store_seen, rows))
        return sleeping

    lifetime = SimpleNamespace(sleeping=sleeping_of)
    tick = SimpleNamespace(LANES=("eng", "ci"), _claimable=claimable_in, lifetime=lifetime)
    box: dict[str, Any] = {"config": "old"}
    state_of = {**_pass(box), "store": store, "config": SimpleNamespace(state=state)}
    assert _run(tasks.DRAINED, tick, state_of, monkeypatch) == expected
    assert box["sleeping"] is sleeping
    assert seen.pop(0) == ("sw", store, "rows")
    assert box["config"] == ("resumed" if expected else "old")
    assert updates == ([("sw", "running")] if expected else [])
    assert seen == [("sw", store, "rows", "doc", lane) for lane in asked]


def test_settle_then_close_space_close_the_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    found = _run(tasks.CLOSE, _tick(calls), _pass(), monkeypatch)
    assert found == ["ran 1", "ran 2"]
    assert calls == [
        ("settle", "sw", "config", "store", "ledger", "rows", "doc"),
        ("close space", "sw", "config", "store", "runtime"),
    ]


def test_a_raising_step_skips_the_rest_and_raises_at_the_site(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    box: dict[str, Any] = {}
    found = _run(tasks.TASK_PASSES, _tick(calls, raises=difficulty.size_pass), _pass(box), monkeypatch)
    assert found == ["ran 1", "ran 2"]
    assert [call[0] for call in calls] == ["run window", claim_hold.verification_notices, difficulty.size_pass]
    assert list(box) == [tasks.RAISED]
    with pytest.raises(RuntimeError, match="^boom$"):
        tasks.raise_held(box)


def test_a_held_system_exit_is_raised_again() -> None:
    def leave(tick: Any, get: Any) -> Any:
        raise SystemExit(3)

    box: dict[str, Any] = {}
    assert tasks.held(leave)(None, {"box": box}.get) == []
    with pytest.raises(SystemExit) as raised:
        tasks.raise_held(box)
    assert raised.value.code == 3


def test_a_held_step_gets_the_tick_and_lookup_it_was_called_with() -> None:
    seen: list[Any] = []
    tick = object()

    def step(given: Any, get: Any) -> list[str]:
        seen.append(given)
        return [get("name")]

    assert tasks.held(step)(tick, {"box": {}, "name": "ran"}.get) == ["ran"]
    assert seen == [tick]


def test_an_empty_box_raises_nothing() -> None:
    assert tasks.raise_held({}) is None
    assert tasks.RAISED == "raised"
