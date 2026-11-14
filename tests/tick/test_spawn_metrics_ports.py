"""The gate log, metrics spool and process table the spawn pass reaches through the tick metrics and processes ports."""

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scripts.gates import Who
from scripts.gates import log as gate_log
from scripts.swarm import dispatcher, metrics
from scripts.swarm_v2.runtime import codex
from scripts.tick.adapters.calls import LocalMetrics, MetricsCalls
from scripts.tick.adapters.fakes import FakeMetrics, FakeProcesses, fake_tools
from scripts.tick.adapters.metrics import SpoolMetrics
from scripts.tick.deadline import Deadline
from scripts.tick.ports import UndeclaredEffect
from scripts.tick.step import Context
from scripts.tick.steps import place

pytestmark = pytest.mark.unit

DEADLINE = Deadline(10, lambda: 0)
EXPIRED = Deadline(0, lambda: 0)


def _refuse(*args: object, **kwargs: object) -> None:
    raise AssertionError("the spawn pass reached the host gate log, metrics spool or process table directly")


def _ports(answers: dict[str, object] | None = None) -> tuple[FakeMetrics, FakeProcesses, MetricsCalls]:
    measured, scanned = FakeMetrics(), FakeProcesses(answers or {})
    return measured, scanned, MetricsCalls(measured, scanned, DEADLINE)


@pytest.fixture
def held(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[FakeMetrics, FakeProcesses]]:
    monkeypatch.setattr(gate_log, "append", _refuse)
    monkeypatch.setattr(metrics, "record", _refuse)
    monkeypatch.setattr(codex, "processes", _refuse)
    measured, scanned, calls = _ports({"rescan": {41: "agent"}})
    token = place.PASS_METRICS.set(calls)
    yield measured, scanned
    place.PASS_METRICS.reset(token)


@pytest.mark.feature("tick-spawn-metrics-processes")
def test_a_failing_claim_cap_writes_its_fail_open_row_through_the_metrics_port(
    monkeypatch: pytest.MonkeyPatch, held: tuple[FakeMetrics, FakeProcesses]
) -> None:
    def broken(*args: object) -> str:
        raise RuntimeError("store down")

    monkeypatch.setattr(place.tick, "_claim_cap", broken)

    assert place._lives_spent("sw", None, None, {}, {"id": "t1"}) == ""

    measured, _ = held
    [call] = measured.calls
    [row] = cast(tuple[gate_log.Row, ...], call.args[1])
    assert (call.operation, call.args[0], call.deadline) == ("log", "sw", DEADLINE)
    assert (row.gate, row.kind, row.agent, row.task, row.reason) == (
        place.claim_cap.GATE.name,
        "fail-open",
        "swarm",
        "t1",
        "RuntimeError: store down",
    )


@pytest.mark.feature("tick-spawn-metrics-processes")
def test_an_observed_claim_cap_writes_its_row_through_the_metrics_port(
    monkeypatch: pytest.MonkeyPatch, held: tuple[FakeMetrics, FakeProcesses]
) -> None:
    store = SimpleNamespace(
        claims=lambda slug, task_id: place.claim_cap.CAP,
        config=lambda slug: SimpleNamespace(gates={}),
        handoff_envelope=lambda slug, task_id: {"reason": "quota"},
        launches=lambda slug: [],
    )
    monkeypatch.setattr(place.modes, "configured", lambda gate, gates: "observe")
    monkeypatch.setattr(place.claim_cap, "refusal", lambda *args: "four lives spent")

    assert place._claim_cap("sw", store, None, {}, {"id": "t1"}) == ""

    measured, _ = held
    [call] = measured.calls
    [row] = cast(tuple[gate_log.Row, ...], call.args[1])
    assert (call.operation, call.args[0]) == ("log", "sw")
    assert (row.gate, row.kind, row.task, row.reason) == (
        place.claim_cap.GATE.name,
        "observe",
        "t1",
        "four lives spent",
    )


@pytest.mark.feature("tick-spawn-metrics-processes")
def test_the_dispatcher_log_writes_gate_and_dispatch_rows_through_the_port() -> None:
    measured, _, calls = _ports()
    action = dispatcher.Action(dispatcher.CHAIN, "apply", "chain claimed", {"id": "t1"})

    token = dispatcher.PORT.set(calls)
    try:
        assert dispatcher.log("sw", [action], 9) == []
    finally:
        dispatcher.PORT.reset(token)

    assert [call.operation for call in measured.calls] == ["log", "emit"]
    [row] = cast(tuple[gate_log.Row, ...], measured.calls[0].args[1])
    assert row == gate_log.Row(
        9, dispatcher.AUTHOR, "apply", dispatcher.AUTHOR, "t1", dispatcher.CHAIN, "chain claimed"
    )
    assert measured.calls[1].args == ({dispatcher.TABLE.name: [dispatcher.dispatch_row("sw", 0, action, 9)]},)


@pytest.mark.feature("tick-spawn-metrics-processes")
def test_the_dispatcher_log_outside_a_pass_writes_the_host_gate_log_and_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    landed: list[tuple[str, object]] = []
    recorded: list[tuple[object, object, int]] = []
    monkeypatch.setattr(gate_log, "append", lambda slug, row: landed.append((slug, row)))
    monkeypatch.setattr(metrics, "record", lambda table, rows, now_ms: recorded.append((table, rows, now_ms)) or [])
    action = dispatcher.Action(dispatcher.CHAIN, "apply", "chain claimed", {"id": "t1"})

    assert dispatcher.log("sw", [action], 9) == []

    row = gate_log.Row(9, dispatcher.AUTHOR, "apply", dispatcher.AUTHOR, "t1", dispatcher.CHAIN, "chain claimed")
    assert landed == [("sw", row)]
    assert recorded == [(dispatcher.TABLE, [dispatcher.dispatch_row("sw", 0, action, 9)], 9)]


@pytest.mark.feature("tick-spawn-metrics-processes")
def test_a_failed_metrics_emit_reports_like_the_outbox() -> None:
    calls = MetricsCalls(SpoolMetrics(cast(Any, None), {}), FakeProcesses(), DEADLINE)

    assert calls.record(dispatcher.TABLE, []) == ["metrics outbox failed: 'unknown metrics table: dispatch_actions'"]


@pytest.mark.feature("tick-spawn-metrics-processes")
def test_the_spool_metrics_appends_each_gate_row_to_the_gate_log(monkeypatch: pytest.MonkeyPatch) -> None:
    landed: list[tuple[str, object]] = []
    monkeypatch.setattr(gate_log, "append", lambda slug, row: landed.append((slug, row)))
    spool = SpoolMetrics(cast(Any, None), {})

    assert spool.log("sw", ["a", "b"], deadline=DEADLINE) == 2
    assert landed == [("sw", "a"), ("sw", "b")]
    with pytest.raises(TimeoutError):
        spool.log("sw", ["c"], deadline=EXPIRED)
    assert landed == [("sw", "a"), ("sw", "b")]


@pytest.mark.feature("tick-spawn-metrics-processes")
def test_the_gate_log_write_needs_the_metrics_effect_and_a_rescan_needs_none() -> None:
    tools = fake_tools(frozenset())

    with pytest.raises(UndeclaredEffect):
        tools.metrics.log("sw", ["row"], deadline=DEADLINE)
    assert tools.processes.rescan(deadline=DEADLINE) is None
    calls = cast(FakeProcesses, cast(Any, tools.processes).adapter).calls
    assert [(call.operation, call.args, call.deadline) for call in calls] == [("rescan", (), DEADLINE)]


@pytest.mark.feature("tick-spawn-metrics-processes")
def test_the_codex_legacy_admission_reads_the_table_it_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex, "processes", _refuse)
    monkeypatch.setattr(codex.process, "local_namespace", lambda: "ns")
    seen: list[object] = []

    def target(pid: object, namespace: str, table: object) -> dict[str, object]:
        seen.append((pid, namespace, table))
        raise codex.SwarmError("stop here")

    monkeypatch.setattr(codex.process, "launch_target", target)
    agent = SimpleNamespace(
        harness="codex",
        transport=codex.LEGACY_TRANSPORT,
        execution_id="",
        profile_decision={"validation": {"pid": 41}},
        pane_id="%1",
    )
    retired: list[object] = []
    runtime = SimpleNamespace(retire=retired.append)

    with pytest.raises(codex.SpawnError, match="stop here"):
        codex.admit_legacy(
            cast(Any, None), "sw", cast(Any, agent), cast(Any, runtime), cast(Any, lambda: {41: "agent"})
        )

    assert seen == [(41, "ns", {41: "agent"})]
    assert retired == [agent]


@pytest.mark.feature("tick-spawn-metrics-processes")
def test_spawn_helpers_outside_a_step_use_the_local_gate_log_and_process_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    landed: list[tuple[str, object]] = []
    monkeypatch.setattr(gate_log, "append", lambda slug, row: landed.append((slug, row)))
    monkeypatch.setattr("hooks.proc.processes", lambda: {7: "self"})

    ports = place._ports()
    ports.log("sw", ["row"])

    assert isinstance(ports, LocalMetrics)
    assert landed == [("sw", "row")]
    assert ports.table() == {7: "self"}


