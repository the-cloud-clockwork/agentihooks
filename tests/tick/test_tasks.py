"""The task steps run the tick's hand written task passes, settle and close space at their sites in today's order."""

from types import SimpleNamespace
from typing import Any

import pytest

import scripts.swarm.cli  # noqa: F401
from scripts.swarm import (
    claim_hold,
    dev_red,
    difficulty,
    grouping,
    prerequisite_wait,
    promotion_watch,
    release_wait,
)
from scripts.tick import engine, registry
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain
from scripts.tick.steps import PASS, CallStep, actions, agents, budgets, tasks

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


MOVED = ((agents, ("_reap", "_strays", "_conversations", "_session_models")),)


def _run(group: str, tick: SimpleNamespace, state: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setattr("scripts.tick.steps.import_module", lambda name: tick)
    for module, names in MOVED:
        for name in names:
            if hasattr(tick, name):
                monkeypatch.setattr(module, name, getattr(tick, name))
            else:
                monkeypatch.delattr(module, name)
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
    assert [step.name for step in engine.group_steps(tasks.CLOSE) if step in tasks.STEPS] == ["settle", "close space"]
    assert [step.group for step in tasks.STEPS] == (
        [tasks.DRAINED] * 2 + [tasks.REOPENS] * 5 + [tasks.TASK_PASSES] * 5 + [tasks.CLOSE] * 2
    )
    registered = [step for wave in registry.waves() for step in wave]
    for step in tasks.STEPS:
        assert step in registered
        slow = {"fleet red notice", "promotion refresh", "release alert"}
        lane = engine.SLOW if step.name in slow else CallStep.lane
        assert (step.lane, step.budget_s) == (lane, budgets.timing_of(step.name)[0])


def test_a_write_step_runs_in_the_lane_it_is_given() -> None:
    step = tasks.WriteStep("fleet red notice", tasks.REOPENS, lambda tick, get: [], lane=engine.SLOW)
    assert (step.lane, tasks.WriteStep("rank pass", tasks.REOPENS, lambda tick, get: []).lane) == (
        engine.SLOW,
        CallStep.lane,
    )


def test_the_task_passes_run_their_calls_with_the_pass_state(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    tick = _tick(calls)
    found = _run(tasks.TASK_PASSES, tick, _pass(), monkeypatch)
    assert found == ["ran 1", "ran 2", "ran 3", "ran 4", "ran 5"]
    assert calls == [
        ("run window", "sw", "config", "store", "ledger", "doc", 7),
        (claim_hold.verification_notices, "sw", "store", "doc", 7),
        (tasks.size_writes, tick, "doc"),
        (tasks.release_writes, tick, "sw", "store", "doc"),
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
        (tasks.fleet_red_writes, "sw", "store", "rows", 7),
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


@pytest.mark.parametrize(("box", "expected"), [({"sleeping": False}, False), ({"sleeping": True}, True), ({}, True)])
def test_a_swarm_reads_asleep_when_the_sleeping_step_left_no_answer(box: dict[str, Any], expected: bool) -> None:
    assert tasks.sleeping_of(box) is expected


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
    found = _run(tasks.TASK_PASSES, _tick(calls, raises=tasks.size_writes), _pass(box), monkeypatch)
    assert found == ["ran 1", "ran 2"]
    assert [call[0] for call in calls] == ["run window", claim_hold.verification_notices, tasks.size_writes]
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


def test_a_placing_step_gets_the_tick_and_lookup_inside_the_host_placement_lock() -> None:
    seen: list[Any] = []

    class Lock:
        def __enter__(self) -> None:
            seen.append("locked")

        def __exit__(self, *args: object) -> None:
            seen.append("unlocked")

    tick = SimpleNamespace(PLACING=Lock())

    def call(found: Any, get: Any) -> list[str]:
        seen.append((found, get("slug")))
        return ["placed"]

    assert tasks.placing(call)(tick, {"slug": "sw"}.get) == ["placed"]
    assert seen == ["locked", (tick, "sw"), "unlocked"]
