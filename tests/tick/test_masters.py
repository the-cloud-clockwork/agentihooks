"""The master and dispatcher seat steps run the tick's hand written seat calls at their sites in today's order."""

from time import monotonic
from types import SimpleNamespace
from typing import Any, cast

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
from scripts.tick.step import Context, Domain, Result, Write
from scripts.tick.steps import PASS, actions, actions_of, masters

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
        _master=_master,
    )


def _master(*args: Any) -> tuple[Any, ...]:
    return ("master", *args)


def _step(name: str) -> Any:
    return next(step for step in masters.STEPS if step.name == name)


def _call(name: str, state: dict[str, Any]) -> tuple[list[tuple[Any, ...]], Any]:
    calls: list[tuple[Any, ...]] = []
    found = _step(name).call(_tick(calls), state.get)
    return calls, found


def test_the_steps_sit_at_their_sites_in_todays_order() -> None:
    sites = [(step.name, step.group) for step in masters.STEPS]
    assert sites == [
        ("master wake", "masters"),
        ("retire idle master", "masters"),
        ("master stall", "masters"),
        ("prompt stall", "masters"),
        ("master down", "master seats"),
        ("tick master", "master seats"),
        ("master scale", "master seats"),
        ("seat alarm", "master seats"),
        ("dispatch seat", "dispatch seat"),
        ("night watch", "master watch"),
        ("master alarm", "master watch"),
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


def test_a_held_step_hands_its_call_the_tick_and_the_reader() -> None:
    seen: list[tuple[Any, Any]] = []
    get = {"box": {}}.get

    def record(tick: Any, reader: Any) -> list[str]:
        seen.append((tick, reader))
        return ["done"]

    assert masters._held(record)("tick", get) == ["done"]
    assert seen == [("tick", get)]


def test_the_site_view_reads_the_locals_and_the_box_for_the_pass_domain() -> None:
    box: dict[str, Any] = {}
    view = masters._SiteView({"slug": "sw", "box": "shadowed"}, box)
    assert view.read(PASS, "slug") == "sw"
    assert view.read(PASS, "box") is box
    assert view.read(Domain("tasks"), "slug") is None
    with pytest.raises(KeyError, match="doc"):
        view.read(PASS, "doc")


def test_a_site_runs_its_group_against_the_locals_under_the_step_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, Context]] = []

    def run_group(name: str, ctx: Context) -> tuple[engine.StepRecord, ...]:
        seen.append((name, ctx))
        result = Result((Write(actions_of("one"), "one", ["ran"]),))
        return (engine.StepRecord("one", 0.0, 1.0, result=result),)

    monkeypatch.setattr(engine, "run_group", run_group)
    assert masters.run_site("master watch", {"slug": "sw"}) == ["ran"]
    ((name, ctx),) = seen
    assert name == "master watch"
    assert ctx.view.read(PASS, "slug") == "sw"
    assert ctx.view.read(PASS, "box") == {}
    assert ctx.deadline.expires_at - monotonic() == pytest.approx(600.0, abs=5.0)


def test_a_site_raises_what_its_steps_held(monkeypatch: pytest.MonkeyPatch) -> None:
    def run_group(name: str, ctx: Context) -> tuple[engine.StepRecord, ...]:
        cast(dict[str, Any], ctx.view.read(PASS, "box"))[masters.RAISED] = KeyError("held")
        return ()

    monkeypatch.setattr(engine, "run_group", run_group)
    with pytest.raises(KeyError, match="held"):
        masters.run_site("masters", {})


@pytest.mark.parametrize(
    ("site", "group"),
    [
        (masters.run_masters, "masters"),
        (masters.run_seats, "master seats"),
        (masters.run_dispatch, "dispatch seat"),
        (masters.run_watch, "master watch"),
    ],
)
def test_each_site_names_its_group(site: Any, group: str, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(engine, "run_group", lambda name, ctx: seen.append(name) or ())
    assert site({}) == []
    assert seen == [group]
