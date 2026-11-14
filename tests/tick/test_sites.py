"""The sites of the flat pass graph: the graph view, the tick's checks between sites and the graph runner."""

from threading import Event
from time import monotonic
from typing import Any

import pytest

from scripts.swarm import reaper
from scripts.swarm import tick as tick_module
from scripts.swarm.store import RedisStore
from scripts.tick import engine, sites
from scripts.tick.deadline import Deadline
from scripts.tick.step import Domain, Write
from scripts.tick.steps import PASS, PassBox, actions, actions_of
from scripts.tick.steps import masters as master_steps
from scripts.tick.steps import tasks as task_steps
from tests.swarm.test_tick import store  # noqa: F401
from tests.swarm.test_tick_groups import _graphs, _Marker, _recorder, _with

pytestmark = pytest.mark.unit


def test_a_graph_view_reads_its_box_then_the_pass_state_for_the_pass_domain_only() -> None:
    box = {"slug": "boxed", "count": 0, "sleeping": False}
    view = sites._GraphView({"slug": "sw", "now_ms": 5, "count": 3}, box)
    assert view.read(PASS, "box") is box
    assert view.read(PASS, "slug") == "boxed"
    assert view.read(PASS, "count") == 0
    assert view.read(PASS, "now_ms") == 5
    assert view.read(PASS, "missing") is None
    assert view.read(PASS, "sleeping") is False
    assert sites._GraphView({"sleeping": False}, {}).read(PASS, "sleeping") is True
    assert view.read(Domain("tasks"), "slug") is None
    assert view.read(Domain("tasks"), "box") is None


def _tick_sites(box: dict[str, Any], **state: Any) -> sites.TickSites:
    return sites.TickSites(state, box, {}, lambda: 7)


def test_tick_sites_shut_every_step_once_a_step_held_an_exception() -> None:
    found = _tick_sites({task_steps.RAISED: RuntimeError("held")})
    for group in ("start", "observe", "wrapper relay"):
        assert found.shut(_Marker("m", group))


def test_tick_sites_shut_tick_sites_past_the_start_when_finished_or_left() -> None:
    box: dict[str, Any] = {"sleeping": False}
    found = _tick_sites(box)
    assert not found.shut(_Marker("m", "observe"))
    box["finished"] = True
    assert found.shut(_Marker("m", "observe"))
    assert not found.shut(_Marker("m", "start"))
    assert not found.shut(_Marker("m", "wrapper relay"))
    del box["finished"]
    found.left = True
    assert found.shut(_Marker("m", "close"))
    assert not found.shut(_Marker("m", "wrapper view"))


def test_tick_sites_shut_the_placement_sites_while_the_swarm_sleeps() -> None:
    box: dict[str, Any] = {"sleeping": True}
    found = _tick_sites(box)
    assert found.shut(_Marker("m", master_steps.SEATS))
    assert found.shut(_Marker("m", "placement"))
    assert not found.shut(_Marker("m", "dispatch seat"))
    box["sleeping"] = False
    assert not found.shut(_Marker("m", master_steps.SEATS))
    assert not found.shut(_Marker("m", "placement"))


def test_tick_sites_leave_the_tick_out_when_a_value_it_reads_is_missing() -> None:
    box: dict[str, Any] = {"config": 1, "doc": 2}
    found = _tick_sites(box)
    hooks = found.hooks()
    assert set(hooks) == {"start", "agents", "tasks"}
    assert hooks["agents"]() is False
    assert found.left
    box["rows"] = 3
    assert hooks["agents"]() is True
    assert not found.left
    del box["config"]
    assert hooks["tasks"]() is False
    assert found.left
    box["config"] = 1
    assert hooks["tasks"]() is True
    assert not found.left


def test_the_start_check_reads_the_clock_adds_the_appliers_and_scans_once(
    store: RedisStore,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = {7: object()}
    monkeypatch.setattr(reaper, "processes", lambda: table)
    state: dict[str, Any] = {"store": store, "slug": "sw"}
    found = sites.TickSites(state, {}, {}, lambda: 7, scanning=True)
    before = reaper.PASS.get()
    assert found.hooks()["start"]() is True
    assert state["now_ms"] == 7
    assert set(found.apply) == set(task_steps.appliers(state))
    assert found.scan is not None
    scanning = reaper.PASS.get()
    assert scanning is not before and scanning is not None
    assert scanning.scan(deadline=Deadline.after(5.0)) is table
    found.close()
    assert found.scan is None
    assert reaper.PASS.get() is before
    found.close()
    assert reaper.PASS.get() is before


def test_the_start_check_starts_no_scan_unless_asked(store: RedisStore) -> None:  # noqa: F811
    found = sites.TickSites({"store": store, "slug": "sw"}, {}, {}, lambda: 7)
    before = reaper.PASS.get()
    assert found.hooks()["start"]() is True
    assert found.scan is None
    assert reaper.PASS.get() is before


def test_run_sites_runs_the_registered_steps_of_its_sites_in_site_order_through_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[str] = []
    later = frozenset({"c1"})
    earlier = frozenset(actions_of(name) for name in ("a1", "a2", "b1"))
    found = [
        _Marker(name, group, _recorder(ran, lambda get, name=name: name, [name]), needs=needs, reads=reads)
        for name, group, needs, reads in (
            ("c1", "c", frozenset(), earlier),
            ("b1", "b", later, frozenset()),
            ("a1", "a", later, frozenset()),
            ("a2", "a", later, frozenset()),
            ("x1", "x", frozenset(), frozenset()),
        )
    ]
    _with(monkeypatch, *found, real=False)
    seen = _graphs(monkeypatch)
    gate = engine.SiteGate({"b": lambda: True}, lambda step: step.name == "a2")
    box: dict[str, Any] = {}
    records = sites.run_sites(("a", "b", "c"), {"slug": "sw"}, box, gate, {})
    assert [record.name for record in records] == ["a1", "a2", "b1", "c1"]
    assert [record.rested for record in records] == [False, True, False, False]
    assert ran == ["a1", "b1", "c1"]
    assert actions(records) == ["a1", "b1", "c1"]
    [(steps, ctx, _)] = seen
    needs = {step.name: step.needs for step in steps}
    assert needs == {"a1": frozenset(), "a2": frozenset(), "b1": frozenset(), "c1": frozenset({"a1", "a2", "b1"})}
    assert ctx.view.read(PASS, "slug") == "sw"
    assert ctx.view.read(PASS, "box") is box
    assert ctx.deadline.expires_at - monotonic() == pytest.approx(tick_module.GROUP_BUDGET_S, abs=5.0)


def test_run_sites_applies_writes_through_its_appliers_and_notes_the_steps_it_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = Event()
    applied: list[Write] = []

    def stuck(get: Any) -> list[str]:
        release.wait(30.0)
        return []

    _with(monkeypatch, _Marker("a1", "a", lambda get: ["a1"]), _Marker("b1", "b", stuck, budget_s=0.05), real=False)
    box = PassBox()
    apply = {actions_of("a1"): lambda write: applied.append(write) or None}
    try:
        sites.run_sites(("a", "b"), {}, box, engine.SiteGate({}, lambda step: False), apply)
        assert [write.value for write in applied] == [["a1"]]
        assert box.left == ["b1"]
    finally:
        release.set()
        worker = engine._RUNNING.get((None, "b1"))
        if worker is not None:
            worker.join(30.0)


def test_run_sites_prints_what_the_slow_lane_steps_hand_back(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _with(monkeypatch, _Marker("a1", "a", lambda get: ["a1"]), real=False)
    finished: list[Any] = []
    real = engine.run_graph

    def run_graph(steps: Any, ctx: Any, apply: Any, done: Any, gate: Any) -> Any:
        records = real(steps, ctx, apply, done, gate)
        finished.append(done)
        return records

    monkeypatch.setattr(engine, "run_graph", run_graph)
    box: dict[str, Any] = {"raised": RuntimeError("held")}
    records = sites.run_sites(("a",), {}, box, engine.SiteGate({}, lambda step: False), {})
    capsys.readouterr()
    [done] = finished
    done(records)
    assert capsys.readouterr().out == "a1\na slow lane step held RuntimeError('held')\n"
