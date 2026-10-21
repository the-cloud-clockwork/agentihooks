import json
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.codex_session import protocol, supervisor
from scripts.codex_session.binding import CONNECTED, BindingStore, TransportRecord
from scripts.swarm import idle, live_binding, runtime
from scripts.swarm.store import APP_SERVER_TRANSPORT, AgentRecord, RedisStore, SwarmConfig

pytestmark = pytest.mark.unit
SLUG = "managed"


def _close_only(args):
    if args[:2] == ["agent", "get"]:
        return {}
    if args[:2] != ["pane", "close"]:
        raise AssertionError("managed execution must not inspect a pane")
    return {}


def _endpoint(rig, endpoint_ref):
    moved = replace(rig.record, endpoint_ref=endpoint_ref, state=CONNECTED)
    rig.store.redis.hset(rig.store.key(SLUG, "codex-transport"), moved.execution_id, json.dumps(asdict(moved)))


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
    monkeypatch.setattr("scripts.herdr_panes.load", lambda environ: [])
    monkeypatch.setattr(live_binding, "read", Mock(return_value={"process": True}))
    from scripts import terminate_agent

    monkeypatch.setattr(terminate_agent, "sessions", lambda: [])
    herdr = Mock(side_effect=_close_only)
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


def test_managed_retirement_uses_the_server_start_identity(rig, tmp_path, monkeypatch):
    from scripts.swarm import reaper

    _launched(monkeypatch, rig, "viewer", "tv")
    rig.herdr.side_effect = _panes({"viewer": "tv"})
    rig.runner.end = Mock(return_value=reaper.Outcome((123,)))
    assert rig.runner.retire(rig.agent, homes=(tmp_path,))
    rig.runner.end.assert_called_once_with(rig.agent.name, 123, [], 456)
    assert rig.bindings.get(SLUG, rig.agent.execution_id).state == "exited"
    assert rig.bindings.get(SLUG, rig.agent.execution_id).active_turn_id == ""
    assert not rig.store.redis.hgetall(rig.store.key(SLUG, "codex-endpoints"))
    assert [call.args[0] for call in rig.herdr.call_args_list] == [
        ["pane", "list"],
        ["agent", "get", "viewer"],
        ["pane", "close", "viewer"],
    ]


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


def test_managed_retirement_closes_every_recorded_viewer_and_removes_the_runtime_home(rig, tmp_path):
    from scripts.swarm import reaper

    path = supervisor.socket_path(tmp_path / supervisor.RUNTIME_SUBDIR, rig.agent.execution_id)
    (path.parent / "codex").mkdir()
    _endpoint(rig, f"unix://{path}")
    rig.bindings.add_viewer(SLUG, rig.agent.execution_id, "w2:p9", "t9")
    rig.bindings.add_viewer(SLUG, rig.agent.execution_id, "viewer", "tv")
    rig.bindings.add_viewer(SLUG, rig.agent.execution_id, "w2:p5", "t5")
    rig.bindings.add_viewer(SLUG, rig.agent.execution_id, "w2:p7", "t7")
    rig.bindings.add_viewer(SLUG, rig.agent.execution_id, "w2:p8", "")
    rig.bindings.add_viewer(SLUG, "exe-other", "w2:p6", "t6")
    listed = [("viewer", "tv"), ("w2:p5", "t5"), ("w2:p6", "t6"), ("w2:p7", "reused"), ("w2:p8", ""), ("w2:p9", "t9")]
    panes = {"panes": [{"pane_id": pane, "terminal_id": terminal} for pane, terminal in listed]}
    rig.herdr.side_effect = lambda args: panes if args == ["pane", "list"] else _close_only(args)
    rig.runner.end = Mock(return_value=reaper.Outcome((123,)))
    assert rig.runner.retire(rig.agent)
    assert [call.args[0] for call in rig.herdr.call_args_list] == [
        ["pane", "list"],
        ["agent", "get", "viewer"],
        ["pane", "close", "viewer"],
        ["pane", "close", "w2:p5"],
        ["pane", "close", "w2:p9"],
    ]
    assert rig.bindings.viewers(SLUG, rig.agent.execution_id) == {}
    assert rig.bindings.viewers(SLUG, "exe-other") == {"w2:p6": "t6"}
    assert not path.parent.exists()
    assert tmp_path.exists()


@pytest.mark.parametrize(("parent", "name"), [("cx", "shared"), ("shared", "own"), ("shared", "shared")])
def test_managed_retirement_keeps_a_folder_outside_the_endpoint_layout(rig, tmp_path, parent, name):
    from scripts.swarm import reaper

    own = rig.agent.execution_id.removeprefix(supervisor.EXECUTION_PREFIX)[: supervisor.ENDPOINT_ID_CHARS]
    foreign = tmp_path / parent / (own if name == "own" else name)
    foreign.mkdir(parents=True)
    _endpoint(rig, f"unix://{foreign}/s")
    rig.runner.end = Mock(return_value=reaper.Outcome((123,)))
    assert rig.runner.retire(rig.agent)
    assert foreign.exists()


def test_a_viewer_pane_that_will_not_close_refuses_the_retirement(rig, monkeypatch):
    from scripts.swarm import reaper

    def herdr(args):
        if args == ["pane", "list"]:
            return {"panes": [{"pane_id": "viewer", "terminal_id": "tv"}]}
        raise RuntimeError("herdr is down")

    _launched(monkeypatch, rig, "viewer", "tv")
    rig.herdr.side_effect = herdr
    rig.runner.end = Mock(return_value=reaper.Outcome((123,)))
    assert not rig.runner.retire(rig.agent)
    assert rig.runner.refusal(rig.agent) == {"process": 123, "refusal": "pane viewer: herdr is down"}
    assert rig.bindings.get(SLUG, rig.agent.execution_id).state == CONNECTED


def test_a_pane_list_that_fails_refuses_the_retirement(rig):
    from scripts.swarm import reaper

    rig.bindings.add_viewer(SLUG, rig.agent.execution_id, "w2:p5", "t5")
    rig.herdr.side_effect = RuntimeError("herdr is down")
    rig.runner.end = Mock(return_value=reaper.Outcome((123,)))
    assert not rig.runner.retire(rig.agent)
    assert rig.runner.refusal(rig.agent) == {"process": 123, "refusal": "pane list: herdr is down"}
    assert rig.bindings.get(SLUG, rig.agent.execution_id).state == CONNECTED
    assert rig.bindings.viewers(SLUG, rig.agent.execution_id) == {"w2:p5": "t5"}


def test_a_runtime_home_that_will_not_go_refuses_the_retirement(rig, tmp_path, monkeypatch):
    from scripts.swarm import reaper

    path = supervisor.socket_path(tmp_path / supervisor.RUNTIME_SUBDIR, rig.agent.execution_id)
    _endpoint(rig, f"unix://{path}")
    rig.bindings.add_viewer(SLUG, rig.agent.execution_id, "w2:p5", "t5")
    rig.herdr.side_effect = lambda args: {"panes": []} if args == ["pane", "list"] else _close_only(args)

    def busy(directory):
        raise OSError("busy")

    monkeypatch.setattr(runtime.shutil, "rmtree", busy)
    rig.runner.end = Mock(return_value=reaper.Outcome((123,)))
    assert not rig.runner.retire(rig.agent)
    assert rig.runner.refusal(rig.agent) == {"process": 123, "refusal": "runtime home: busy"}
    assert rig.bindings.get(SLUG, rig.agent.execution_id).state == CONNECTED
    assert rig.bindings.viewers(SLUG, rig.agent.execution_id) == {"w2:p5": "t5"}


def test_a_viewer_pane_already_gone_does_not_refuse_the_retirement(rig):
    from scripts.swarm import reaper

    rig.herdr.side_effect = RuntimeError("pane viewer not found")
    rig.runner.end = Mock(return_value=reaper.Outcome((123,)))
    assert rig.runner.retire(rig.agent)


def test_a_headless_retirement_with_its_runtime_home_already_gone_succeeds(rig, tmp_path):
    from scripts.swarm import reaper

    directory = rig.agent.execution_id.removeprefix(supervisor.EXECUTION_PREFIX)[: supervisor.ENDPOINT_ID_CHARS]
    _endpoint(rig, f"unix://{tmp_path}/{supervisor.RUNTIME_SUBDIR}/{directory}/s")
    headless = replace(rig.agent, pane_id="")
    rig.runner.end = Mock(return_value=reaper.Outcome((123,)))
    assert rig.runner.retire(headless)
    rig.herdr.assert_not_called()
    assert rig.runner.refusal(headless) == {"process": 0, "refusal": "unknown"}


@pytest.mark.parametrize(("owner", "closed"), [("engineer@other-0009", False), ("", True)])
def test_managed_retirement_closes_the_launch_pane_only_while_it_belongs_to_the_worker(rig, owner, closed):
    from scripts.swarm import reaper

    def herdr(args):
        if args[:2] == ["agent", "get"]:
            return {"agent": {"pane_id": "viewer", "name": owner or rig.agent.name, "agent_session": None}}
        if args == ["pane", "list"]:
            return {"panes": [{"pane_id": "viewer", "terminal_id": "tv"}]}
        return _close_only(args)

    rig.bindings.add_viewer(SLUG, rig.agent.execution_id, "viewer", "tv")
    rig.herdr.side_effect = herdr
    rig.runner.end = Mock(return_value=reaper.Outcome((123,)))
    assert rig.runner.retire(rig.agent)
    assert (["pane", "close", "viewer"] in [call.args[0] for call in rig.herdr.call_args_list]) is closed
    assert rig.bindings.get(SLUG, rig.agent.execution_id).state == "exited"


def _panes(listed):
    def herdr(args):
        if args == ["pane", "list"]:
            return {"panes": [{"pane_id": pane, "terminal_id": terminal} for pane, terminal in listed.items()]}
        if args[:2] == ["agent", "get"]:
            raise RuntimeError("agent not found")
        return _close_only(args)

    return herdr


def _launched(monkeypatch, rig, pane, terminal):
    from scripts import herdr_panes

    record = SimpleNamespace(pane_id=pane, terminal_id=terminal, kind="init-agent", owner_session=rig.agent.name)
    monkeypatch.setattr(herdr_panes, "load", lambda environ: [record])


def test_managed_retirement_leaves_a_reused_pane_on_another_terminal_open(rig, monkeypatch):
    from scripts.swarm import reaper

    _launched(monkeypatch, rig, "viewer", "launch")
    rig.herdr.side_effect = _panes({"viewer": "operator-shell"})
    rig.runner.end = Mock(return_value=reaper.Outcome((123,)))
    assert rig.runner.retire(rig.agent)
    assert ["pane", "close", "viewer"] not in [call.args[0] for call in rig.herdr.call_args_list]
    assert rig.bindings.get(SLUG, rig.agent.execution_id).state == "exited"


def test_managed_retirement_leaves_a_pane_with_no_recorded_terminal_open(rig):
    from scripts.swarm import reaper

    rig.herdr.side_effect = _panes({"viewer": "operator-shell"})
    rig.runner.end = Mock(return_value=reaper.Outcome((123,)))
    assert rig.runner.retire(rig.agent)
    rig.herdr.assert_not_called()
    assert rig.bindings.get(SLUG, rig.agent.execution_id).state == "exited"


def test_managed_retirement_after_a_reopen_closes_the_earlier_launch_pane(rig, monkeypatch):
    from scripts.swarm import reaper

    _launched(monkeypatch, rig, "viewer", "launch")
    reopened = replace(rig.agent, pane_id="w2:p7")
    rig.bindings.add_viewer(SLUG, rig.agent.execution_id, "w2:p7", "reopened")
    rig.herdr.side_effect = _panes({"viewer": "launch", "w2:p7": "reopened"})
    rig.runner.end = Mock(return_value=reaper.Outcome((123,)))
    assert rig.runner.retire(reopened)
    closed = [call.args[0][2] for call in rig.herdr.call_args_list if call.args[0][:2] == ["pane", "close"]]
    assert sorted(closed) == ["viewer", "w2:p7"]
