"""The placement steps run the tick's hand written acks, ranking, preempt and spawn calls at their sites in today's order."""

import runpy
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import pytest

import scripts.swarm.cli  # noqa: F401
from scripts.swarm import bottleneck_remedy, panic
from scripts.tick import engine, registry
from scripts.tick.adapters.fakes import fake_tools
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain
from scripts.tick.steps import (
    AGENTS,
    PASS,
    CallStep,
    actions,
    actions_of,
    budgets,
    pass_domains,
    place,
    tasks,
)

BOXED = ("config", "rows", "doc", "sleeping")

pytestmark = pytest.mark.unit


class _State:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state

    def read(self, domain: Domain, key: str) -> object | None:
        return self.state.get(key) if domain == PASS else None


LOCK_KEY = "sw:acks-lock"


class _Redis:
    def __init__(self, log: list[Any]) -> None:
        self.log = log
        self.token = ""

    def delete(self, key: str) -> None:
        if key == LOCK_KEY:
            self.token = ""
        else:
            self.log.append(("delete", key))

    def set(self, key: str, value: str, nx: bool, px: int) -> bool:
        assert (key, nx, px) == (LOCK_KEY, True, 30_000)
        if self.log.count("acks held"):
            return False
        self.token = value
        return True

    def get(self, key: str) -> str:
        assert key == LOCK_KEY
        return self.token


def _store(log: list[Any]) -> SimpleNamespace:
    return SimpleNamespace(redis=_Redis(log), key=lambda slug, name: f"{slug}:{name}")


ROWS = {"t1": {"id": "t1", "state": "open"}}


def _probe(runtime: Any, *args: Any) -> None:
    return None


def _pass(log: list[Any], state: str = "running", engine_name: str = "tick", **extra: Any) -> dict[str, Any]:
    return {
        "slug": "sw",
        "config": SimpleNamespace(state=state, engine=engine_name),
        "store": _store(log),
        "ledger": "ledger",
        "runtime": "runtime",
        "rows": ROWS,
        "doc": "doc",
        "now_ms": 7,
        "due": "due key",
        "box": {},
        "sleeping": False,
    } | extra


def _tick(log: list[Any]) -> SimpleNamespace:
    def skip_refused(function: Any, *args: Any) -> list[str]:
        log.append((function, *args))
        return [f"ran {function if isinstance(function, str) else function.__name__}"]

    class _Seats:
        def __init__(self, *args: Any) -> None:
            self.args = args

        def __eq__(self, other: object) -> bool:
            return isinstance(other, _Seats) and self.args == other.args

    return SimpleNamespace(
        skip_refused=skip_refused,
        PLACING=nullcontext(),
        EVENTS_ENGINE="events",
        _codex_hook_order="codex hook order",
        _ended="ended",
        _current_owner="owner",
        ready_in_order="ready in order",
        claim_view="claim view",
        preempt=SimpleNamespace(probe=_probe, Seats=_Seats, run="preempt"),
        bottleneck_remedy=bottleneck_remedy,
        panic=panic,
    )


def _run(
    group: str, tick: SimpleNamespace, state: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tools: Any = None
) -> list[str]:
    monkeypatch.setattr("scripts.tick.steps.import_module", lambda name: tick)
    ctx = Context(_State(state), Deadline.after(60.0), tools or fake_tools())
    return actions(engine.run_group(group, ctx))


@pytest.fixture
def calm(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    log: list[Any] = []
    monkeypatch.setattr(panic, "state", lambda store: log.append("panic read") and None)
    monkeypatch.setattr(bottleneck_remedy, "step", "bottleneck remedy")
    return log


def test_the_placement_steps_sit_at_their_sites_in_legacy_order() -> None:
    assert (place.ACKS, place.PLACEMENT) == ("placement acks", "placement")
    assert [step.name for step in engine.group_steps(place.ACKS)] == ["codex hook order"]
    assert [step.name for step in engine.group_steps(place.PLACEMENT)] == [
        "rank pass",
        "bottleneck remedy",
        "preempt",
    ]
    declared = {
        step.name: (step.needs, step.reads - pass_domains(*BOXED), step.writes - {actions_of(step.name)})
        for step in place.STEPS
    }
    assert declared["preempt"] == (place.PREEMPT_NEEDS, frozenset({AGENTS}), frozenset({AGENTS}))
    assert declared["rank pass"] == (frozenset({"drained resume"}), frozenset(), frozenset({tasks.RANK}))
    assert {name: value[1:] for name, value in declared.items() if name not in {"preempt", "rank pass"}} == {
        name: (frozenset(), frozenset())
        for name in (
            "quota notice",
            "lost runner",
            "time left",
            "codex hook order",
            "bottleneck remedy",
        )
    }
    assert [step.name for step in engine.group_steps(place.NOTICES)] == ["quota notice", "lost runner", "time left"]
    registered = [step for wave in registry.waves() for step in wave]
    for step in place.STEPS:
        assert step in registered
        lane = engine.SLOW if step.name in {"lost runner", "time left"} else CallStep.lane
        assert (step.lane, step.budget_s) == (lane, budgets.timing_of(step.name)[0])


def test_placement_requires_the_report_steps_its_spawn_step_needs(monkeypatch: pytest.MonkeyPatch) -> None:
    required: list[str] = []
    monkeypatch.setattr(registry, "registry", registry.Registry())
    monkeypatch.setattr(registry, "require", required.append)
    runpy.run_module("scripts.tick.steps.place")
    assert required == ["scripts.tick.steps.report"]
    assert {"overrun pass", "quiet pass"} <= budgets.SPAWN_NEEDS


def test_the_placement_notices_run_their_calls_with_the_pass_state(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import lost_runner, quota_notice, time_left

    calls: list[tuple[Any, ...]] = []

    def skip_refused(function: Any, *args: Any) -> list[str]:
        calls.append((function, *args))
        return [f"ran {len(calls)}"]

    state = {
        "slug": "sw",
        "config": "config",
        "store": "store",
        "ledger": "ledger",
        "runtime": "runtime",
        "doc": "doc",
        "now_ms": 7,
        "box": {},
    }
    assert _run(
        place.NOTICES, SimpleNamespace(skip_refused=skip_refused, PLACING=nullcontext()), state, monkeypatch
    ) == [
        "ran 1",
        "ran 2",
        "ran 3",
    ]
    assert calls == [
        (quota_notice.refresh, "sw", "config", "store", "ledger", "runtime", 7),
        (lost_runner.sweep, "sw", "config", "store", 7),
        (time_left.refresh, "sw", "store", "ledger", "runtime", "config", "doc"),
    ]


@pytest.mark.parametrize(("state", "engine_name"), [("paused", "tick"), ("running", "events")])
def test_preempt_waits_for_a_running_tick_engine(
    calm: list[Any], monkeypatch: pytest.MonkeyPatch, state: str, engine_name: str
) -> None:
    found = _run(place.PLACEMENT, _tick(calm), _pass(calm, state, engine_name), monkeypatch)
    assert found == ["ran rank_writes", "ran bottleneck remedy"]
    assert "preempt" not in [entry[0] for entry in calm if isinstance(entry, tuple)]


def test_a_panic_holds_preempt_and_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[Any] = []
    monkeypatch.setattr(panic, "state", lambda store: "halted")
    monkeypatch.setattr(bottleneck_remedy, "step", "bottleneck remedy")
    found = _run(place.PLACEMENT, _tick(log), _pass(log), monkeypatch)
    assert found == ["ran rank_writes", "ran bottleneck remedy"]


def test_a_held_exception_skips_every_placement_step(calm: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    state = _pass(calm, box={tasks.RAISED: RuntimeError("earlier")})
    assert _run(place.PLACEMENT, _tick(calm), state, monkeypatch) == []
    assert calm == []


SPAWN_CLUSTER = (
    "_lives_spent",
    "_claim_cap",
    "_free_seat",
    "primed",
    "placed_record",
    "_host_full",
    "host_spent",
    "_spend_host",
    "_spend_local",
    "_release_hive_launch",
    "_hold",
    "spawn_holds",
)
