"""The expert seats tick step registers with its group, lane, feature and ports, runs the expert seats pass, holds
while its swarm's panic is on and holds the exception the pass raises."""

from importlib import import_module
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import cli, expert_seat
from scripts.tick import registry
from scripts.tick.steps import RAISED, experts, pass_domains

pytestmark = pytest.mark.unit


def _state() -> dict[str, Any]:
    return {
        "slug": "sw",
        "config": SimpleNamespace(state="running"),
        "store": "store",
        "runtime": "runtime",
        "now_ms": 7,
        "sleeping": True,
        "box": {},
    }


def _step() -> Any:
    return {step.name: step for step in registry.steps()}["expert seats"]


def _swarm(monkeypatch: pytest.MonkeyPatch, panic: list[object], calls: list[tuple[Any, ...]]) -> None:
    """Stand in for what the step takes from the swarm command module: the panic state and the fenced call."""
    monkeypatch.setattr(cli, "panic", SimpleNamespace(state=lambda store, slug: panic[0] if slug == "sw" else None))
    monkeypatch.setattr(cli, "skip_refused", lambda function, *args: calls.append((function, *args)) or ["seated"])


@pytest.mark.feature("expert-seat-step")
def test_expert_seats() -> None:
    """The expert seats step registers in group dispatch seat on the tick lane with its feature and ports, after the
    drained resume, reading the config and placing."""
    import_module("scripts.tick.steps.experts")
    found = _step()
    assert (found.group, found.lane, found.feature, found.effects) == (
        "dispatch seat",
        "tick",
        "expert-seat-step",
        frozenset({"coordination", "inbox", "runtime"}),
    )
    assert found.needs == frozenset({"drained resume", "reap"})
    assert found.reads == pass_domains("config")
    assert found.placing is True


@pytest.mark.feature("expert-seat-step")
def test_the_step_runs_the_expert_seats_pass_and_nothing_while_panic_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    panic: list[object] = ["halted"]
    _swarm(monkeypatch, panic, calls)
    state = _state()
    step = _step()
    assert step.call(None, state.get) == []
    assert calls == [] and RAISED not in state["box"]
    panic[0] = None
    assert step.call(None, state.get) == ["seated"]
    assert calls == [(expert_seat.run, "sw", state["config"], "store", "runtime", 7)]


def test_a_sleeping_swarm_still_keeps_its_expert_seats(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    _swarm(monkeypatch, [None], calls)
    state = _state()
    step = _step()
    assert state["sleeping"] is True
    assert step.call(None, state.get) == ["seated"]
    assert "sleeping" not in step.needs and step.reads == pass_domains("config")


def test_the_step_holds_the_exception_its_pass_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    failure = RuntimeError("the store is gone")

    def failing(*args: Any) -> list[str]:
        raise failure

    monkeypatch.setattr(experts, "SEAT", SimpleNamespace(expert_seat=SimpleNamespace(run=failing)))
    monkeypatch.setattr(cli, "panic", SimpleNamespace(state=lambda store, slug: None))
    monkeypatch.setattr(cli, "skip_refused", lambda function, *args: function(*args))
    state = _state()
    step = _step()
    assert step.call(None, state.get) == []
    assert state["box"][RAISED] is failure
