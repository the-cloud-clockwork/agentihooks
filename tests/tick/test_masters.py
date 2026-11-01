"""The master and dispatcher seat steps run the tick's hand written seat calls at their sites in today's order."""

from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import (
    dispatch_seat,
    lifetime,
    master_alarm,
    master_prompt_stall,
    master_stall,
    master_wake,
    tick_master,
)
from scripts.swarm_v2 import master_scale, master_seat_alarm
from scripts.tick import engine, registry
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain
from scripts.tick.steps import PASS, actions, masters

pytestmark = pytest.mark.unit


class _State:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state

    def read(self, domain: Domain, key: str) -> object | None:
        return self.state.get(key) if domain == PASS else None


def _pass(state: str = "running") -> dict[str, Any]:
    return {
        "slug": "sw",
        "store": "store",
        "ledger": "ledger",
        "runtime": "runtime",
        "now_ms": 7,
        "box": {},
        "config": SimpleNamespace(state=state),
        "doc": "doc",
        "rows": "rows",
        "sleeping": False,
    }


def _tick(calls: list[tuple[Any, ...]]) -> SimpleNamespace:
    def skip_refused(function: Any, *args: Any) -> list[str]:
        calls.append((function, *args))
        return [f"ran {len(calls)}"]

    return SimpleNamespace(
        skip_refused=skip_refused,
        _master_down="master down",
        _night_watch="night watch",
        _master=lambda *args: ("master", *args),
    )


def _step(name: str) -> Any:
    return next(step for step in masters.STEPS if step.name == name)


def _call(name: str, state: dict[str, Any]) -> tuple[list[tuple[Any, ...]], Any]:
    calls: list[tuple[Any, ...]] = []
    found = _step(name).call(_tick(calls), state.get)
    return calls, found


def test_the_steps_sit_at_their_sites_in_todays_order() -> None:
    sites = [(step.name, step.group, step.order) for step in masters.STEPS]
    assert sites == [
        ("master wake", "masters", 10),
        ("retire idle master", "masters", 20),
        ("master stall", "masters", 30),
        ("prompt stall", "masters", 40),
        ("master down", "master seats", 10),
        ("tick master", "master seats", 20),
        ("master scale", "master seats", 30),
        ("seat alarm", "master seats", 40),
        ("dispatch seat", "dispatch seat", 10),
        ("night watch", "master watch", 10),
        ("master alarm", "master watch", 20),
    ]
    registered = [step.name for wave in registry.waves() for step in wave]
    assert all(step.name in registered for step in masters.STEPS)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("master wake", (master_wake.run, "sw", "store", "runtime", "doc", 7)),
        ("retire idle master", (lifetime.retire_idle_master, "sw", "store", "ledger", "runtime", "rows", 7)),
        ("master stall", (master_stall.run, "sw", "CONFIG", "store", "ledger", "runtime", 7)),
        ("prompt stall", (master_prompt_stall.run, "sw", "CONFIG", "store", "ledger", "runtime", 7)),
        ("master down", ("master down", "sw", "CONFIG", "store", "ledger", "runtime", 7)),
        ("master scale", (master_scale.run, "sw", "CONFIG", "store", "runtime", "doc", 7)),
        ("seat alarm", (master_seat_alarm.run, "sw", "store", "ledger", "runtime", "doc")),
        ("dispatch seat", (dispatch_seat.run, "sw", "CONFIG", "store", "runtime", "doc", 7, False)),
        ("night watch", ("night watch", "sw", "store", "ledger", "runtime", "doc", 7, False)),
    ],
)
def test_each_step_runs_its_call_with_the_pass_state(name: str, expected: tuple[Any, ...]) -> None:
    state = _pass()
    calls, found = _call(name, state)
    config = state["config"]
    assert calls == [tuple(config if part == "CONFIG" else part for part in expected)]
    assert found == ["ran 1"]


def test_master_wake_skips_a_stopped_or_stopping_swarm() -> None:
    for state in ("stopped", "stopping"):
        calls, found = _call("master wake", _pass(state))
        assert (calls, found) == ([], [])


def test_tick_master_hands_the_master_start_to_its_call() -> None:
    state = _pass()
    calls, found = _call("tick master", state)
    function, *args, start = calls[0]
    assert (function, args) == (tick_master.run, ["sw", state["config"], "store", "ledger", "runtime", 7])
    assert start() == ("master", "sw", state["config"], "store", "runtime", 7)
    assert found == ["ran 1"]


def test_master_alarm_passes_the_promoted_master(monkeypatch: pytest.MonkeyPatch) -> None:
    promoted: list[tuple[Any, ...]] = []
    monkeypatch.setattr(tick_master, "promoted", lambda *args: promoted.append(args) or "promoted")
    calls, found = _call("master alarm", _pass())
    assert promoted == [("store", "sw")]
    assert calls == [(master_alarm.run, "sw", "store", "runtime", "promoted")]
    assert found == ["ran 1"]


def test_a_raising_step_is_held_and_the_rest_of_its_group_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []

    def boom(*args: Any) -> list[str]:
        raise RuntimeError("down")

    monkeypatch.setattr(master_wake, "run", boom)
    monkeypatch.setattr(lifetime, "retire_idle_master", lambda *args: ran.append("retire") or ["retired"])
    state = _pass()
    records = engine.run_group("masters", Context(_State(state), Deadline.after(60.0)), masters.STEPS)
    assert actions(records) == []
    assert ran == []
    assert isinstance(state["box"][masters.RAISED], RuntimeError)
    with pytest.raises(RuntimeError, match="down"):
        masters.raise_held(state["box"])


def test_raise_held_passes_when_nothing_was_held() -> None:
    masters.raise_held({})


def test_a_system_exit_is_held_too() -> None:
    box: dict[str, Any] = {}

    def leave(tick: Any, get: Any) -> list[str]:
        raise SystemExit(3)

    held = masters._held(leave)
    assert held(None, {"box": box}.get) == []
    assert isinstance(box[masters.RAISED], SystemExit)
