"""The observe step's launch outcome helpers retire, report, relaunch or expire a launch through the tick seams."""

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.hive import reports
from scripts.swarm import failed_panes, launch_check, live_binding, master_retire, master_start, reaper, tick
from scripts.swarm import idle as idle_state
from scripts.swarm.store import MASTER, AgentRecord
from scripts.tick.steps import observe

pytestmark = pytest.mark.unit


class _Recorder:
    """Record each method call into a shared list and answer from a table keyed by method name."""

    redis: "_Recorder"

    def __init__(self, calls: list[tuple[Any, ...]], answers: dict[str, Any], prefix: str = "") -> None:
        self.calls, self.answers, self.prefix = calls, answers, prefix

    def __getattr__(self, name: str) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((self.prefix + name, args, kwargs))
            return self.answers.get(name)

        return call


def _store(calls: list[tuple[Any, ...]], answers: dict[str, Any] | None = None) -> _Recorder:
    store = _Recorder(calls, {"key": "sw:launch-assignments", "config": "config", **(answers or {})})
    store.redis = _Recorder(calls, {}, "redis.")
    return store


def _seams(monkeypatch: pytest.MonkeyPatch, calls: list[tuple[Any, ...]], retired: bool = True) -> None:
    monkeypatch.setattr(tick, "_retire_launch", lambda *args: calls.append(("retire", args)) or retired)
    monkeypatch.setattr(tick, "_drop", lambda *args: calls.append(("drop", args)) or " and dropped")
    monkeypatch.setattr(tick, "_settle_admission", lambda *args: calls.append(("settle", args)))
    monkeypatch.setattr(tick, "_failed_hive_launch", lambda *args: calls.append(("hive", args)) or "hive failed")
    monkeypatch.setattr(launch_check, "session_started_at", lambda agent: 1_000)
    monkeypatch.setattr(launch_check, "record", lambda *args, **kwargs: calls.append(("record", args, kwargs)))
    monkeypatch.setattr(launch_check, "forget", lambda *args: calls.append(("forget", args)))
    monkeypatch.setattr(launch_check, "told", lambda found, outcome: f"told {sorted(found)} {outcome}")
    monkeypatch.setattr(launch_check, "REPORT_ONLY", frozenset({"late"}))


def _miss(agent: AgentRecord, found: dict[str, Any], relaunch: bool) -> SimpleNamespace:
    return SimpleNamespace(agent=agent, found=found, relaunch=relaunch)


@pytest.mark.feature("swarm-tick-launch-cluster")
@pytest.mark.parametrize("lane", ["eng", MASTER])
def test_a_failed_hive_launch_retires_reports_and_drops_its_agent(monkeypatch: pytest.MonkeyPatch, lane: str) -> None:
    calls: list[tuple[Any, ...]] = []
    _seams(monkeypatch, calls)
    store, ledger = _store(calls), _Recorder(calls, {}, "ledger.")
    agent, rows = AgentRecord("engineer@1-1", lane, "t1"), {"t1": {}}
    said = observe._failed_hive_launch("sw", store, ledger, "runtime", rows, _miss(agent, {"pane": 1}, True), 5_000)
    assert said == "retired engineer@1-1 after its hive join failed and dropped"
    assert calls == [
        ("retire", ("sw", store, "runtime", agent, 5_000)),
        ("record", (store, "sw", agent, {"pane": 1}, 5_000, 4_000), {}),
        ("forget", (store, "sw", "engineer@1-1")),
        *([("ledger.notify", ("sw", "told ['pane'] retired"), {})] if lane == MASTER else []),
        ("drop", ("sw", store, ledger, rows, agent)),
    ]


@pytest.mark.feature("swarm-tick-launch-cluster")
def test_a_failed_hive_launch_that_cannot_retire_retries_next_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    _seams(monkeypatch, calls, retired=False)
    store, agent = _store(calls), AgentRecord("engineer@1-1", "eng", "t1")
    said = observe._failed_hive_launch("sw", store, "ledger", "runtime", {}, _miss(agent, {"pane": 1}, True), 5_000)
    assert said == "could not retire engineer@1-1 after its hive join failed, retrying next tick"
    assert calls == [("retire", ("sw", store, "runtime", agent, 5_000))]


def _launch_seams(
    monkeypatch: pytest.MonkeyPatch, calls: list[tuple[Any, ...]], working: bool, relaunched: bool
) -> None:
    _seams(monkeypatch, calls)
    monkeypatch.setattr(idle_state, "heartbeat", lambda redis, slug, name: ("beat", slug, name))
    monkeypatch.setattr(
        idle_state,
        "verdict",
        lambda pane, beat, wait, now_ms: (
            idle_state.WORKING if working and beat == ("beat", "sw", "engineer@1-1") else "idle"
        ),
    )
    monkeypatch.setattr(reports, "host", lambda agent: "")
    monkeypatch.setattr(launch_check, "relaunched", lambda store, slug, task: relaunched)
    monkeypatch.setattr(launch_check, "mark_relaunched", lambda *args: calls.append(("mark", args)))
    monkeypatch.setattr(master_retire, "hold", lambda *args: "")
    monkeypatch.setattr(
        live_binding, "relaunch_assignment", lambda agent, task, config: {"task": agent.task, "config": config}
    )
    monkeypatch.setattr(master_start, "read", lambda store, slug: {"name": "engineer@1-1", "at": 1})
    monkeypatch.setattr(master_start, "save", lambda *args: calls.append(("master save", args)))


@pytest.mark.feature("swarm-tick-launch-cluster")
def test_a_failed_launch_check_on_a_working_agent_is_only_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    _launch_seams(monkeypatch, calls, working=True, relaunched=False)
    store, agent = _store(calls), AgentRecord("engineer@1-1", "eng", "t1")
    said = observe._failed_launch(
        "sw", store, "ledger", "runtime", {}, _miss(agent, {"pane": 1, "model": 2}, True), 5_000
    )
    assert said == "engineer@1-1 failed its launch check on pane, model; reported only while working"
    assert calls == [
        ("record", (store, "sw", agent, {"pane": 1, "model": 2}, 5_000, 4_000), {"held": True}),
        ("forget", (store, "sw", "engineer@1-1")),
    ]


@pytest.mark.feature("swarm-tick-launch-cluster")
@pytest.mark.parametrize(("found", "relaunch", "relaunched"), [({"pane": 1}, False, False), ({"pane": 1}, True, True)])
def test_a_hive_agent_failing_an_enforced_check_hands_over_to_the_hive_failure(
    monkeypatch: pytest.MonkeyPatch, found: dict[str, Any], relaunch: bool, relaunched: bool
) -> None:
    calls: list[tuple[Any, ...]] = []
    _launch_seams(monkeypatch, calls, working=False, relaunched=relaunched)
    store, agent, rows = _store(calls), AgentRecord("engineer@1-1", "eng", "t1", hive="h1"), {"t1": {}}
    miss = _miss(agent, found, relaunch)
    assert observe._failed_launch("sw", store, "ledger", "runtime", rows, miss, 5_000) == "hive failed"
    assert calls == [("hive", ("sw", store, "ledger", "runtime", rows, miss, 5_000))]


@pytest.mark.feature("swarm-tick-launch-cluster")
def test_a_hive_agent_late_on_a_report_only_check_settles_its_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    _launch_seams(monkeypatch, calls, working=False, relaunched=False)
    store, agent = _store(calls), AgentRecord("engineer@1-1", "eng", "t1", hive="h1")
    said = observe._failed_launch("sw", store, "ledger", "runtime", {}, _miss(agent, {"late": 1}, True), 5_000)
    assert said == "engineer@1-1 failed its launch check on late; reported only"
    assert calls == [
        ("settle", ("sw", "runtime", agent)),
        ("record", (store, "sw", agent, {"late": 1}, 5_000, 4_000), {"held": False}),
        ("forget", (store, "sw", "engineer@1-1")),
    ]


@pytest.mark.feature("swarm-tick-launch-cluster")
def test_a_local_launch_failure_after_its_relaunch_is_spent_is_only_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    _launch_seams(monkeypatch, calls, working=False, relaunched=True)
    store, agent = _store(calls), AgentRecord("engineer@1-1", "eng", "t1")
    runtime = SimpleNamespace(live_names=lambda: set())
    result = observe._failed_launch("sw", store, "ledger", runtime, {}, _miss(agent, {"pane": 1}, True), 5_000)
    assert result == "engineer@1-1 failed its launch check on pane; its one relaunch is spent"
    assert calls == [
        ("record", (store, "sw", agent, {"pane": 1}, 5_000, 4_000), {"held": True}),
        ("forget", (store, "sw", "engineer@1-1")),
    ]


@pytest.mark.feature("swarm-tick-launch-cluster")
def test_a_local_launch_failure_that_cannot_retire_retries_next_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    _launch_seams(monkeypatch, calls, working=False, relaunched=False)
    monkeypatch.setattr(tick, "_retire_launch", lambda *args: calls.append(("retire", args)) or False)
    store, agent = _store(calls), AgentRecord("engineer@1-1", "eng", "t1")
    runtime = SimpleNamespace(live_names=lambda: set())
    result = observe._failed_launch("sw", store, "ledger", runtime, {}, _miss(agent, {"pane": 1}, True), 5_000)
    assert result == "could not retire engineer@1-1 after its launch check failed on pane, retrying next tick"
    assert calls == [("retire", ("sw", store, runtime, agent, 5_000))]


@pytest.mark.feature("swarm-tick-launch-cluster")
@pytest.mark.parametrize("lane", ["eng", MASTER])
def test_a_relaunched_launch_saves_its_assignment_and_drops_the_agent(
    monkeypatch: pytest.MonkeyPatch, lane: str
) -> None:
    calls: list[tuple[Any, ...]] = []
    _launch_seams(monkeypatch, calls, working=False, relaunched=False)
    store, ledger = _store(calls), _Recorder(calls, {}, "ledger.")
    agent, rows = AgentRecord("engineer@1-1", lane, "t1"), {"t1": {"id": "t1"}}
    runtime = SimpleNamespace(live_names=lambda: set())
    said = observe._failed_launch("sw", store, ledger, runtime, rows, _miss(agent, {"pane": 1, "late": 2}, True), 5_000)
    assert said == "retired engineer@1-1 after its launch check failed on pane, late and dropped"
    assert calls == [
        ("retire", ("sw", store, runtime, agent, 5_000)),
        ("record", (store, "sw", agent, {"pane": 1, "late": 2}, 5_000, 4_000), {"held": False}),
        ("forget", (store, "sw", "engineer@1-1")),
        *([("ledger.notify", ("sw", "told ['pane'] relaunch"), {})] if lane == MASTER else []),
        ("mark", (store, "sw", "t1")),
        ("config", ("sw",), {}),
        ("key", ("sw", "launch-assignments"), {}),
        ("redis.hset", ("sw:launch-assignments", "t1", '{"task": "t1", "config": "config"}'), {}),
        *([("master save", (store, "sw", {"name": "", "at": 5_000, "retry": True}))] if lane == MASTER else []),
        ("drop", ("sw", store, ledger, rows, agent)),
    ]


@pytest.mark.feature("swarm-tick-launch-cluster")
@pytest.mark.parametrize(
    ("lane", "live", "retired", "held"),
    [
        ("eng", False, True, True),
        ("eng", True, True, False),
        (MASTER, False, True, False),
        ("eng", False, False, False),
    ],
)
def test_retiring_a_launch_holds_the_seat_of_an_agent_that_exited_first(
    monkeypatch: pytest.MonkeyPatch, lane: str, live: bool, retired: bool, held: bool
) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(reaper, "scratch_homes", lambda slug, task: [f"{slug}/{task}"])
    monkeypatch.setattr(failed_panes, "hold", lambda *args: calls.append(("hold", args)))
    monkeypatch.setattr(
        observe.retire_watch, "reported_gone", lambda store, agent, now_ms: store != "store" or now_ms != 5
    )
    agent = AgentRecord("engineer@1-1", lane, "t1", pane_id="%3")

    def retire(kept: AgentRecord, homes: list[str]) -> bool:
        calls.append(("retire", kept, homes))
        return retired

    runtime = SimpleNamespace(live_names=lambda: {"engineer@1-1"} if live else set(), retire=retire)
    assert observe._retire_launch("sw", "store", runtime, agent, 5) is retired
    exited = lane != MASTER and not live
    assert calls[0] == ("retire", replace(agent, pane_id="") if exited else agent, ["sw/t1"])
    assert calls[1:] == ([("hold", ("store", "sw", agent, 5, tick.EXITED_BEFORE_LIFE))] if held else [])


@pytest.mark.feature("swarm-tick-launch-cluster")
def test_retiring_a_launch_its_hive_reports_gone_makes_no_runtime_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reaper, "scratch_homes", lambda slug, task: [])
    monkeypatch.setattr(
        observe.retire_watch,
        "reported_gone",
        lambda store, agent, now_ms: (store, agent.name, now_ms) == ("store", MASTER, 5),
    )
    agent = AgentRecord(MASTER, MASTER, MASTER, pane_id="%3")
    runtime = SimpleNamespace(live_names=lambda: set(), retire=lambda kept, homes: pytest.fail("gone needs no retire"))
    assert observe._retire_launch("sw", "store", runtime, agent, 5) is True


@pytest.mark.feature("swarm-tick-launch-cluster")
@pytest.mark.parametrize(
    ("raw", "acknowledging"),
    [
        (None, False),
        ('{"state": "acknowledging"}', True),
        ('{"state": "launching"}', True),
        ('{"state": "started"}', False),
    ],
)
def test_an_acknowledging_launch_is_read_from_the_launch_record(raw: str | None, acknowledging: bool) -> None:
    calls: list[tuple[Any, ...]] = []
    store = _Recorder(calls, {"key": "sw:launches"})
    store.redis = _Recorder(calls, {"hget": raw}, "redis.")
    agent = AgentRecord("engineer@1-1", "eng", "t1")
    assert observe._acknowledging("sw", store, agent) is acknowledging
    assert calls == [("key", ("sw", "launches"), {}), ("redis.hget", ("sw:launches", "engineer@1-1"), {})]
