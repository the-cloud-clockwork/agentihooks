"""The placement steps run the tick's hand written acks, ranking, preempt and spawn calls at their sites in today's order."""

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from scripts.swarm import bottleneck_remedy, panic
from scripts.tick import engine, registry
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain
from scripts.tick.steps import PASS, CallStep, actions, place, tasks

pytestmark = pytest.mark.unit


class _State:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state

    def read(self, domain: Domain, key: str) -> object | None:
        return self.state.get(key) if domain == PASS else None


class _Redis:
    def __init__(self, log: list[Any]) -> None:
        self.log = log

    def delete(self, key: str) -> None:
        self.log.append(("delete", key))


def _probe(runtime: Any, *args: Any) -> None:
    return None


def _pass(log: list[Any], state: str = "running", engine_name: str = "tick", **extra: Any) -> dict[str, Any]:
    return {
        "slug": "sw",
        "config": SimpleNamespace(state=state, engine=engine_name),
        "store": SimpleNamespace(redis=_Redis(log)),
        "ledger": "ledger",
        "runtime": "runtime",
        "rows": "rows",
        "doc": "doc",
        "now_ms": 7,
        "due": "due key",
        "box": {},
        "sleeping": False,
    } | extra


def _tick(log: list[Any], places: bool = True) -> SimpleNamespace:
    def skip_refused(function: Any, *args: Any) -> list[str]:
        log.append((function, *args))
        return [f"ran {function if isinstance(function, str) else function.__name__}"]

    @contextmanager
    def placement_pass(runtime: Any) -> Iterator[None]:
        log.append(("enter pass", runtime))
        yield
        log.append(("leave pass", runtime))

    @contextmanager
    def placing() -> Iterator[None]:
        log.append("enter placing")
        yield
        log.append("leave placing")

    class _Seats:
        def __init__(self, *args: Any) -> None:
            self.args = args

        def __eq__(self, other: object) -> bool:
            return isinstance(other, _Seats) and self.args == other.args

    return SimpleNamespace(
        skip_refused=skip_refused,
        PLACING=placing(),
        EVENTS_ENGINE="events",
        _placement_pass=placement_pass,
        _spawn="spawn",
        _collect_acks="collect acks",
        _codex_hook_order="codex hook order",
        _ended="ended",
        _current_owner="owner",
        ready_in_order="ready in order",
        dispatcher=SimpleNamespace(rank_pass="rank pass"),
        readiness=SimpleNamespace(
            tick_places=lambda engine_name, rows: log.append(("places", engine_name, rows)) or places
        ),
        preempt=SimpleNamespace(probe=_probe, Seats=_Seats, run="preempt"),
        bottleneck_remedy=bottleneck_remedy,
        panic=panic,
    )


def _run(group: str, tick: SimpleNamespace, state: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setattr("scripts.tick.steps.import_module", lambda name: tick)
    return actions(engine.run_group(group, Context(_State(state), Deadline.after(60.0))))


@pytest.fixture
def calm(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    log: list[Any] = []
    monkeypatch.setattr(panic, "state", lambda store: log.append("panic read") and None)
    monkeypatch.setattr(bottleneck_remedy, "step", "bottleneck remedy")
    return log


def test_the_placement_steps_sit_at_their_sites_in_legacy_order() -> None:
    assert (place.ACKS, place.PLACEMENT) == ("placement acks", "placement")
    assert [step.name for step in engine.group_steps(place.ACKS)] == ["collect acks", "codex hook order"]
    assert [step.name for step in engine.group_steps(place.PLACEMENT)] == [
        "rank pass",
        "bottleneck remedy",
        "preempt",
        "spawn",
    ]
    assert [step.order for step in place.STEPS] == [10, 20, 10, 20, 30, 40]
    registered = [step for wave in registry.waves() for step in wave]
    for step in place.STEPS:
        assert step in registered
        assert (step.lane, step.budget_s) == (CallStep.lane, CallStep.budget_s)


def test_collect_acks_then_the_codex_hook_order_while_awake(calm: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    state = _pass(calm)
    found = _run(place.ACKS, _tick(calm), state, monkeypatch)
    assert found == ["ran collect acks", "ran codex hook order"]
    assert calm == [
        ("collect acks", "sw", state["config"], state["store"], "ledger", "runtime", "doc", 7),
        ("codex hook order",),
    ]


def test_a_sleeping_swarm_collects_acks_without_the_codex_hook_order(
    calm: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    found = _run(place.ACKS, _tick(calm), _pass(calm, sleeping=True), monkeypatch)
    assert found == ["ran collect acks"]
    assert [entry[0] for entry in calm] == ["collect acks"]


def test_a_running_pass_ranks_remedies_preempts_and_spawns(calm: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    tick = _tick(calm)
    state = _pass(calm)
    config, store = state["config"], state["store"]
    found = _run(place.PLACEMENT, tick, state, monkeypatch)
    assert found == ["ran rank pass", "ran bottleneck remedy", "ran preempt", "ran spawn"]
    seats = calm[3][-1]
    assert seats.args[:4] == ("rows", "ended", "owner", "ready in order")
    assert seats.args[4].func is _probe
    assert seats.args[4].args == ("runtime",)
    assert calm == [
        ("rank pass", "sw", config, store, "ledger", "doc", 7),
        ("bottleneck remedy", "sw", store, "ledger", "doc", 7),
        "panic read",
        ("preempt", "sw", config, store, "doc", 7, seats),
        ("places", "tick", "rows"),
        "panic read",
        ("enter pass", "runtime"),
        ("spawn", "sw", config, store, "ledger", "runtime", "rows", "doc", 7),
        ("leave pass", "runtime"),
        ("delete", "due key"),
    ]


@pytest.mark.parametrize(("state", "engine_name"), [("paused", "tick"), ("running", "events")])
def test_preempt_waits_for_a_running_tick_engine(
    calm: list[Any], monkeypatch: pytest.MonkeyPatch, state: str, engine_name: str
) -> None:
    found = _run(place.PLACEMENT, _tick(calm, places=False), _pass(calm, state, engine_name), monkeypatch)
    assert found == ["ran rank pass", "ran bottleneck remedy"]
    assert "preempt" not in [entry[0] for entry in calm if isinstance(entry, tuple)]


def test_a_panic_holds_preempt_and_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[Any] = []
    monkeypatch.setattr(panic, "state", lambda store: "halted")
    monkeypatch.setattr(bottleneck_remedy, "step", "bottleneck remedy")
    found = _run(place.PLACEMENT, _tick(log), _pass(log), monkeypatch)
    assert found == ["ran rank pass", "ran bottleneck remedy"]
    assert log[-1] == ("places", "tick", "rows")


def test_spawn_waits_until_the_tick_places(calm: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    found = _run(place.PLACEMENT, _tick(calm, places=False), _pass(calm), monkeypatch)
    assert found == ["ran rank pass", "ran bottleneck remedy", "ran preempt"]
    assert ("delete", "due key") not in calm


def test_a_paused_swarm_does_not_spawn(calm: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    found = _run(place.PLACEMENT, _tick(calm), _pass(calm, "paused"), monkeypatch)
    assert found == ["ran rank pass", "ran bottleneck remedy"]
    assert ("delete", "due key") not in calm


def test_a_held_exception_skips_every_placement_step(calm: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    state = _pass(calm, box={tasks.RAISED: RuntimeError("earlier")})
    assert _run(place.PLACEMENT, _tick(calm), state, monkeypatch) == []
    assert calm == []
