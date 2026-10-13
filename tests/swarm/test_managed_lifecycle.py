from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.codex_session import protocol, supervisor
from scripts.codex_session.binding import CONNECTED, BindingStore, TransportRecord
from scripts.swarm import idle, live_binding, runtime
from scripts.swarm.store import APP_SERVER_TRANSPORT, AgentRecord, RedisStore, SwarmConfig

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]
SLUG = "managed"


@pytest.fixture
def rig(monkeypatch):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, "agentihooks", 1, 0))
    agent = store.start_execution(
        SLUG,
        AgentRecord(
            store.next_name(SLUG, "eng"),
            "eng",
            "task",
            pane_id="viewer",
            harness="codex",
            seat=f"eng-1@{SLUG}",
            transport=APP_SERVER_TRANSPORT,
            profile_decision={"validation": {"pid": 99}},
        ),
    )
    bindings = BindingStore(store)
    record = bindings.reserve(
        SLUG,
        TransportRecord(
            agent.execution_id,
            agent.generation,
            agent.seat,
            agent.name,
            agent.task,
            "unix:///run/managed/server.sock",
            server_pid=123,
            server_start_identity=456,
            thread_id="thread",
        ),
    )
    bindings.put(SLUG, replace(record, state=CONNECTED))
    thread = {"id": "thread", "turns": []}
    connection = Mock()
    connection.request.side_effect = lambda method, params: {"thread": thread}
    connector = Mock(return_value=(connection, {}))
    monkeypatch.setattr(protocol, "connect", connector)
    monkeypatch.setattr(supervisor, "start_time", lambda pid: 456 if pid == 123 else None)
    monkeypatch.setattr(runtime, "connect", lambda: store)
    monkeypatch.setattr(live_binding, "read", Mock(return_value={"process": True}))
    from scripts import terminate_agent

    monkeypatch.setattr(terminate_agent, "sessions", lambda: [])
    herdr = Mock(side_effect=AssertionError("managed execution must not inspect a pane"))
    runner = runtime.HerdrRuntime(herdr=herdr)
    return SimpleNamespace(
        store=store,
        agent=agent,
        bindings=bindings,
        record=record,
        thread=thread,
        connection=connection,
        connector=connector,
        runner=runner,
        herdr=herdr,
    )


def test_managed_server_and_thread_are_live_without_a_viewer(rig):
    assert rig.agent.name in rig.runner.live_names()
    assert rig.runner.reported(rig.agent)
    assert rig.runner.bindings([rig.agent]) == {rig.agent.name: {"process": True}}
    live_binding.read.assert_called_once_with(rig.agent, 123)
    assert rig.runner.status(rig.agent) == "idle"
    assert idle.of(rig.store, rig.runner, SLUG, rig.agent, 1000) == idle.IDLE
    rig.connection.request.assert_called_with("thread/read", {"threadId": "thread", "includeTurns": True})
    rig.connector.assert_called_with("/run/managed/server.sock", 1.0)
    rig.herdr.assert_not_called()


def test_a_legacy_session_without_transport_capability_is_not_managed():
    assert not live_binding.managed(SimpleNamespace(runtime_backend="local"))


def test_runtime_idle_is_distinct_from_productive_waiting(rig):
    idle.declare_wait(rig.store.redis, SLUG, rig.agent.name, 2000, "checks", 1000)
    assert rig.runner.status(rig.agent) == "idle"
    assert idle.of(rig.store, rig.runner, SLUG, rig.agent, 1000) == idle.WAITING
    rig.thread["turns"] = [{"id": "turn", "status": "inProgress"}]
    assert rig.runner.status(rig.agent) == "working"
    assert idle.of(rig.store, rig.runner, SLUG, rig.agent, 1000) == idle.WORKING


def test_server_loss_is_visible_even_when_the_viewer_survives(rig, monkeypatch):
    from scripts import terminate_agent

    monkeypatch.setattr(terminate_agent, "sessions", lambda: [SimpleNamespace(name=rig.agent.name, status="alive")])
    monkeypatch.setattr(supervisor, "start_time", lambda pid: 999)
    assert rig.agent.name not in rig.runner.live_names()
    assert not rig.runner.reported(rig.agent)
    assert rig.runner.status(rig.agent) == "dead"
    assert rig.runner.bindings([rig.agent]) == {rig.agent.name: {"process": False}}


def test_an_unmanaged_execution_without_a_session_is_not_live(rig):
    plain = rig.store.start_execution(
        SLUG,
        AgentRecord(rig.store.next_name(SLUG, "eng"), "eng", "plain", seat=f"eng-2@{SLUG}"),
    )
    assert plain.name not in rig.runner.live_names()


def test_an_unavailable_thread_is_unknown_and_keeps_the_live_execution(rig):
    rig.connection.request.side_effect = protocol.ProtocolError("connection lost")
    assert rig.runner.status(rig.agent) == "unknown"
    assert rig.agent.name in rig.runner.live_names()
    assert not rig.runner.reported(rig.agent)
    assert rig.connection.close.call_count == 2


def test_missing_server_identity_holds_the_execution_without_using_its_viewer(rig):
    rig.bindings.put(SLUG, replace(rig.record, server_start_identity=0))
    assert rig.runner.status(rig.agent) == "unknown"
    assert rig.agent.name in rig.runner.live_names()
    assert rig.runner.bindings([rig.agent]) == {rig.agent.name: {"process": None}}
    rig.herdr.assert_not_called()


@pytest.mark.parametrize("state", ["exited", "failed"])
def test_terminal_transport_is_dead_even_with_a_matching_live_server(rig, state):
    rig.bindings.put(SLUG, replace(rig.record, state=state))
    assert rig.runner.status(rig.agent) == "dead"
    assert not rig.runner.reported(rig.agent)
    assert rig.agent.name not in rig.runner.live_names()


def test_a_different_thread_is_unknown(rig):
    rig.thread["id"] = "foreign"
    assert rig.runner.status(rig.agent) == "unknown"
    assert not rig.runner.reported(rig.agent)


def test_an_open_server_request_is_productive_waiting(rig):
    rig.bindings.put(SLUG, replace(rig.record, state=CONNECTED, pending_request="approval"))
    assert rig.runner.status(rig.agent) == "waiting"
    assert idle.of(rig.store, rig.runner, SLUG, rig.agent, 1000) == idle.WAITING


def test_no_transport_record_is_unknown_and_retirement_is_refused(rig, monkeypatch):
    monkeypatch.setattr(BindingStore, "get", Mock(return_value=None))
    assert rig.runner.status(rig.agent) == "unknown"
    assert not rig.runner.retire(rig.agent)
    assert rig.runner.refusal(rig.agent) == {"process": 0, "refusal": "managed server identity is unknown"}


@pytest.mark.parametrize("field", ["execution_generation", "agent_name", "seat"])
def test_foreign_transport_identity_is_unknown(rig, monkeypatch, field):
    changed = 2 if field == "execution_generation" else "foreign"
    monkeypatch.setattr(BindingStore, "get", Mock(return_value=replace(rig.record, **{field: changed})))
    assert rig.runner.status(rig.agent) == "unknown"


def test_a_replaced_execution_is_not_observed_through_its_old_transport(rig):
    successor = replace(rig.agent, name=rig.store.next_name(SLUG, "eng"), execution_id="", generation=0)
    rig.store.start_execution(SLUG, successor, rig.agent.execution_id)
    assert rig.runner.status(rig.agent) == "unknown"
    assert not rig.runner.reported(rig.agent)


def test_binding_observation_includes_every_managed_execution(rig):
    second = rig.store.start_execution(
        SLUG,
        AgentRecord(
            rig.store.next_name(SLUG, "eng"),
            "eng",
            "second",
            seat=f"eng-2@{SLUG}",
            transport=APP_SERVER_TRANSPORT,
        ),
    )
    rig.bindings.reserve(
        SLUG,
        replace(
            rig.record,
            execution_id=second.execution_id,
            execution_generation=second.generation,
            agent_name=second.name,
            task_id=second.task,
            seat=second.seat,
            endpoint_ref="unix:///run/managed/second",
        ),
    )
    assert rig.runner.bindings([rig.agent, second]) == {
        rig.agent.name: {"process": True},
        second.name: {"process": True},
    }


def test_refused_retirement_keeps_transport_and_reports_the_failed_process(rig):
    from scripts.swarm import reaper

    rig.runner.end = Mock(return_value=reaper.Outcome(process=123, refusal="permission refused"))
    assert not rig.runner.retire(rig.agent)
    assert rig.runner.refusal(rig.agent) == {"process": 123, "refusal": "permission refused"}
    assert rig.bindings.get(SLUG, rig.agent.execution_id).state == CONNECTED


def test_a_successful_retirement_clears_its_previous_refusal_and_active_turn(rig):
    from scripts.swarm import reaper

    rig.bindings.put(SLUG, replace(rig.record, state=CONNECTED, active_turn_id="turn"))
    rig.runner.end = Mock(return_value=reaper.Outcome(process=123, refusal="permission refused"))
    assert not rig.runner.retire(rig.agent)
    rig.runner.end = Mock(return_value=reaper.Outcome((123,)))
    assert rig.runner.retire(rig.agent)
    assert rig.runner.refusal(rig.agent) == {"process": 0, "refusal": "unknown"}
    assert rig.bindings.get(SLUG, rig.agent.execution_id).active_turn_id == ""


def test_managed_conversation_discovery_uses_the_recorded_thread(rig):
    assert rig.runner.conversations() == {"viewer": "thread"}


def test_managed_retirement_uses_the_server_start_identity(rig, tmp_path):
    from scripts.swarm import reaper

    rig.runner.end = Mock(return_value=reaper.Outcome((123,)))
    assert rig.runner.retire(rig.agent, homes=(tmp_path,))
    rig.runner.end.assert_called_once_with(rig.agent.name, 123, [], 456)
    assert rig.bindings.get(SLUG, rig.agent.execution_id).state == "exited"
    assert rig.bindings.get(SLUG, rig.agent.execution_id).active_turn_id == ""
    assert not rig.store.redis.hgetall(rig.store.key(SLUG, "codex-endpoints"))
    rig.herdr.assert_not_called()


def test_managed_retire_process_uses_the_server_instead_of_the_launcher(rig):
    from scripts.swarm import reaper

    rig.runner.end = Mock(return_value=reaper.Outcome((123,)))
    assert rig.runner.retire_process(rig.agent, 99, (), 100)
    rig.runner.end.assert_called_once_with(rig.agent.name, 123, [], 456)


def test_managed_retirement_refuses_an_unknown_start_identity(rig):
    rig.bindings.put(SLUG, replace(rig.record, server_start_identity=0))
    rig.runner.end = Mock()
    assert not rig.runner.retire(rig.agent)
    rig.runner.end.assert_not_called()
    assert rig.runner.refusal(rig.agent) == {"process": 0, "refusal": "managed server identity is unknown"}


def test_local_managed_retirement_does_not_need_a_live_launcher(rig):
    from scripts.swarm import reaper
    from scripts.swarm_v2.runtime.local import LocalHerdrRuntime

    rig.runner.end = Mock(return_value=reaper.Outcome((123,)))
    local = LocalHerdrRuntime(rig.runner, table=Mock(side_effect=AssertionError("launcher is unavailable")))
    assert local.terminate(rig.agent).ok
    rig.runner.end.assert_called_once_with(rig.agent.name, 123, [], 456)


def test_an_unrelated_named_session_survives_managed_retirement(rig, monkeypatch):
    import os
    import subprocess
    import sys

    from hooks.observability.trace_flush import start_time

    monkeypatch.setattr(supervisor, "start_time", start_time)
    argv = [sys.executable, "-c", "import time; time.sleep(300)"]
    env = {**os.environ, "AGENTIHOOKS_AGENT_NAME": rig.agent.name}
    with subprocess.Popen(argv, env=env, start_new_session=True) as server:
        with subprocess.Popen(argv, env=env, start_new_session=True) as unrelated:
            try:
                record = replace(rig.record, server_pid=server.pid, server_start_identity=start_time(server.pid))
                rig.bindings.put(SLUG, record)
                assert rig.runner.retire(rig.agent)
                server.wait(timeout=5)
                assert unrelated.poll() is None
            finally:
                server.terminate()
                unrelated.terminate()
