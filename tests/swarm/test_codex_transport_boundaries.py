from dataclasses import replace
from types import SimpleNamespace

import pytest

from scripts.swarm import seat_spawn, tick
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm.tick import Placed, SpawnError
from scripts.swarm_v2.runtime import codex
from scripts.swarm_v2.runtime.base import LOCAL, Outcome, Status
from scripts.swarm_v2.runtime.routed import RoutedRuntime


@pytest.fixture
def storage(tmp_path, monkeypatch):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", str(tmp_path), 1, 0))
    monkeypatch.setattr(codex, "connect", lambda: store)
    monkeypatch.setattr(codex.process, "local_namespace", lambda: "namespace")
    monkeypatch.setattr(codex, "processes", lambda: {123: SimpleNamespace(start_time=456)})
    return store


def legacy_agent(storage):
    return AgentRecord(
        storage.next_name("sw", "eng", 1000),
        "eng",
        "task",
        harness="codex",
        seat="eng-1@sw",
        pane_id="window:pane",
        transport="legacy",
        profile_decision={"validation": {"pid": 123}},
    )


def test_transport_defaults_remain_legacy_for_older_launch_configurations():
    config = SimpleNamespace()
    placed = Placed("window:pane", "codex")
    assert codex.arguments(config, "codex", {}) == ["--codex-transport", "legacy"]
    assert codex.complete(config, "agent", placed, None) == replace(placed, transport="legacy")


@pytest.mark.parametrize("has_previous", [False, True])
def test_launch_arguments_carry_the_primed_execution_predecessor(storage, has_previous):
    previous = storage.start_execution("sw", legacy_agent(storage)) if has_previous else None
    config = replace(storage.config("sw"), codex_inbox_transport="app_server")
    task = tick.primed(storage, "sw", "eng-1@sw", {"id": "task"})
    assert task["previous_execution_id"] == (previous.execution_id if previous else "")
    expected = ["--codex-transport", "app_server", "--seat", "eng-1@sw"]
    if previous:
        expected += ["--previous-execution", previous.execution_id]
    assert codex.arguments(config, "codex", task) == expected


def test_legacy_admission_replaces_the_predecessor_and_records_process_identity(storage):
    previous = storage.start_execution("sw", legacy_agent(storage))
    retired = []
    agent = legacy_agent(storage)
    admitted = codex.admit_legacy(storage, "sw", agent, SimpleNamespace(retire=retired.append))
    assert admitted.execution_id != previous.execution_id
    assert admitted.generation == previous.generation + 1
    assert admitted.runtime_target == {"process_namespace": "namespace", "pid": 123, "pid_start": 456}
    assert storage.execution_occupants("sw")[agent.seat] == admitted
    assert retired == []


def test_reopening_a_managed_viewer_records_its_new_pane_on_the_execution(storage, monkeypatch):
    from scripts.codex_session import attach
    from scripts.swarm import live_binding

    managed = replace(legacy_agent(storage), transport="app_server", pane_id="w1:p1")
    admitted = storage.start_execution("sw", managed)
    record = SimpleNamespace(state=attach.CONNECTED, thread_id="thread-1", endpoint_ref="unix:///run/codex.sock")
    monkeypatch.setattr(live_binding, "transport", lambda store, agent: record)
    storage.put_agent("sw", replace(admitted, state="idle", idle_ticks=3))
    attach.reopen(storage, "sw", admitted, lambda title, argv: SimpleNamespace(pane_id="w2:p7"), "codex")
    assert storage.execution_occupants("sw")[admitted.seat] == replace(
        admitted, state="idle", idle_ticks=3, pane_id="w2:p7"
    )
    assert [(agent.name, agent.pane_id) for agent in storage.agents("sw")] == [(admitted.name, "w2:p7")]


@pytest.mark.parametrize(
    "harness,transport,execution_id",
    [("claude", "legacy", ""), ("codex", "app_server", ""), ("codex", "legacy", "already-admitted")],
)
def test_admission_leaves_other_transports_and_admitted_executions_unchanged(storage, harness, transport, execution_id):
    agent = replace(legacy_agent(storage), harness=harness, transport=transport, execution_id=execution_id)
    assert codex.admit_legacy(storage, "sw", agent, None) is agent
    assert storage.execution_occupants("sw") == {}


def test_failed_legacy_identity_keeps_the_failure_and_cleanup_target(storage):
    retired = []
    agent = replace(legacy_agent(storage), profile_decision={})
    with pytest.raises(SpawnError) as caught:
        codex.admit_legacy(storage, "sw", agent, SimpleNamespace(retire=retired.append))
    assert str(caught.value) == "local launch refused: its process number and start time could not be read"
    assert caught.value.pane_id == agent.pane_id
    assert retired == [agent]


@pytest.mark.parametrize("fails", [False, True])
def test_shared_seat_launch_admits_its_execution_and_retires_failed_admission(storage, monkeypatch, fails):
    retired = []
    placed = Placed("window:pane", "codex", transport="legacy", profile_decision={"validation": {"pid": 123}})
    runtime = SimpleNamespace(spawn=lambda *_: placed, retire=retired.append)
    if fails:

        def refuse(*args):
            raise SwarmError("admission failed")

        monkeypatch.setattr(storage, "start_execution", refuse)
        with pytest.raises(seat_spawn.SeatFailed) as caught:
            seat_spawn.place("sw", storage.config("sw"), storage, runtime, "dispatch", 1000, lambda r: {"id": r.task})
        assert str(caught.value) == "admission failed"
        assert caught.value.error.pane_id == "window:pane"
        assert [row.name for row in retired] == [caught.value.record.name]
    else:
        record, result = seat_spawn.place(
            "sw", storage.config("sw"), storage, runtime, "dispatch", 1000, lambda r: {"id": r.task}
        )
        assert result.execution == storage.execution_occupants("sw")[record.seat]
        assert result.execution.name == record.name
        assert result.execution.transport == "legacy"
        assert result.pane_id == "window:pane"
        assert retired == []


def test_app_server_completion_preserves_the_admitted_execution_and_transport(storage):
    execution = storage.start_execution("sw", replace(legacy_agent(storage), transport="app_server"))
    placed = Placed("window:pane", "codex")
    config = replace(storage.config("sw"), codex_inbox_transport="app_server")
    result = codex.complete(config, execution.name, placed, None)
    assert result == replace(placed, transport="app_server", execution=execution)


@pytest.mark.parametrize("admitted_transport", [None, "legacy"])
def test_app_server_completion_rejects_missing_or_wrong_transport_with_cleanup(storage, admitted_transport):
    name = "agent"
    if admitted_transport:
        execution = storage.start_execution("sw", replace(legacy_agent(storage), transport=admitted_transport))
        name = execution.name
    ended = []
    config = replace(storage.config("sw"), codex_inbox_transport="app_server")
    with pytest.raises(SpawnError) as caught:
        codex.complete(config, name, Placed("window:pane", "codex"), ended.append)
    assert str(caught.value) == "the Codex app server launch has no matching admitted execution"
    assert caught.value.pane_id == "window:pane"
    assert ended == [name]


@pytest.mark.parametrize("value", [{}, None])
def test_routed_failure_without_a_pane_preserves_empty_cleanup_identity(value):
    router = SimpleNamespace(spawn=lambda _: Outcome("spawn", Status.REFUSED, LOCAL, value, "launch refused"))
    runtime = RoutedRuntime(None, router)
    with pytest.raises(SpawnError) as caught:
        runtime.spawn(None, "eng", "agent", {})
    assert caught.value.pane_id == ""
    assert str(caught.value) == "launch refused"
    assert caught.value.status == Status.REFUSED
