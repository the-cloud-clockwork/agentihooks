import json
import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.swarm_v2 import supervision_agent as agent
from scripts.swarm_v2 import supervision_protocol as protocol
from scripts.swarm_v2 import supervision_runtime as runtime
from scripts.swarm_v2.supervision import Budgets, Launch, LaunchRefused


@pytest.fixture
def supervisor(tmp_path):
    home = tmp_path / "homes" / "codex"
    home.mkdir(parents=True)
    launch = Launch(
        tmp_path, {"execution_id": "admitted"}, home, ("tool",), ("exporter",), ("herdr", "server"), Budgets(2, 1, 1, 1)
    )
    owner = runtime.Supervisor(launch)
    owner.root.mkdir(parents=True)
    protocol.write(owner.root / "context.json", owner.scope)
    return owner


def test_observe_reaps_services_and_accepts_matching_agent_exit(supervisor, monkeypatch):
    child = SimpleNamespace(pid=123, returncode=None)
    supervisor.children["herdr"] = child
    monkeypatch.setattr(runtime.trees, "reap", lambda: [(123, 9), (456, 0)])
    protocol.write(supervisor.root / "agent.exit.json", {**supervisor.scope, "exit_code": 7})
    supervisor.observe()
    assert child.returncode == 9
    assert supervisor.exits == {"herdr": 9, "agent": 7}
    assert supervisor.reaped == {123, 456}


@pytest.mark.parametrize("receipt", [{"exit_code": 0}, {"exit_code": True}, {"exit_code": "0"}])
def test_observe_rejects_unbound_or_noninteger_exit(supervisor, monkeypatch, receipt):
    monkeypatch.setattr(runtime.trees, "reap", lambda: [])
    if receipt["exit_code"] != 0 or type(receipt["exit_code"]) is not int:
        receipt = {**supervisor.scope, **receipt}
    protocol.write(supervisor.root / "agent.exit.json", receipt)
    supervisor.observe()
    assert supervisor.exits == {}


@pytest.mark.parametrize(
    "observed", [None, SimpleNamespace(start_time=8, state="S"), SimpleNamespace(start_time=7, state="Z")]
)
def test_observe_rejects_dead_or_reused_agent(supervisor, monkeypatch, observed):
    monkeypatch.setattr(runtime.trees, "reap", lambda: [])
    probe = Mock(return_value=observed)
    monkeypatch.setattr(runtime, "_process", probe)
    protocol.write(supervisor.root / "agent.json", {**supervisor.scope, "pid": 123, "pid_start": 7})
    supervisor.observe()
    assert supervisor.exits == {"agent": "unknown"}
    probe.assert_called_once_with(123, Path("/proc"))


def test_observe_preserves_living_agent(supervisor, monkeypatch):
    monkeypatch.setattr(runtime.trees, "reap", lambda: [])
    monkeypatch.setattr(runtime, "_process", lambda *_: SimpleNamespace(start_time=7, state="S"))
    protocol.write(supervisor.root / "agent.json", {**supervisor.scope, "pid": 123, "pid_start": 7})
    supervisor.observe()
    assert supervisor.exits == {}


def test_wait_observes_after_bounded_sleep(supervisor, monkeypatch):
    sleep = Mock()
    observe = Mock()
    monkeypatch.setattr(runtime.time, "sleep", sleep)
    monkeypatch.setattr(supervisor, "observe", observe)
    supervisor.wait()
    sleep.assert_called_once_with(0.05)
    observe.assert_called_once_with()


@pytest.mark.parametrize(
    "signum,expected", [(signal.SIGTERM, signal.SIGTERM), (signal.SIGINT, signal.SIGINT), (signal.SIGCHLD, None)]
)
def test_only_termination_signals_request_drain(supervisor, signum, expected):
    supervisor.signal(signum, None)
    assert supervisor.stop == expected


def test_spawn_uses_private_environment_and_independent_session(supervisor, monkeypatch):
    spawn = Mock(return_value=SimpleNamespace(pid=123))
    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    supervisor.spawn("exporter", ("exporter", "--ready"))
    spawn.assert_called_once_with(
        ("exporter", "--ready"),
        env=supervisor.environment,
        cwd=supervisor.launch.attempt,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert supervisor.children["exporter"].pid == 123
    assert supervisor.environment["HOME"] == str(supervisor.launch.home)
    assert supervisor.environment["CODEX_HOME"] == str(supervisor.launch.home / ".codex")
    assert supervisor.environment["CLAUDE_CONFIG_DIR"] == str(supervisor.launch.home / ".claude")


@pytest.mark.parametrize("role", ["agent", "exporter"])
def test_ready_requires_bound_receipt(supervisor, monkeypatch, role):
    monkeypatch.setattr(supervisor, "observe", lambda: None)
    monkeypatch.setattr(runtime.time, "monotonic", Mock(side_effect=[0, 0, 2]))
    protocol.write(supervisor.root / f"{role}.json", {**supervisor.scope, "status": "ready"})
    assert supervisor.ready(role, 1)
    protocol.write(supervisor.root / f"{role}.json", {**supervisor.scope, "incarnation": "old", "status": "ready"})
    monkeypatch.setattr(supervisor, "wait", lambda: setattr(supervisor, "stop", signal.SIGTERM))
    assert not supervisor.ready(role, 1)


def test_ready_retries_transport_failure_until_stopped(supervisor, monkeypatch):
    monkeypatch.setattr(runtime.time, "monotonic", lambda: 0)
    monkeypatch.setattr(supervisor, "observe", lambda: None)
    herdr = Mock(side_effect=subprocess.TimeoutExpired("herdr", 0.5))
    monkeypatch.setattr(supervisor, "herdr", herdr)
    monkeypatch.setattr(supervisor, "wait", lambda: setattr(supervisor, "stop", signal.SIGTERM))
    assert not supervisor.ready("herdr", 1)
    herdr.assert_called_once_with(["workspace", "list"])


def test_ready_fails_on_service_exit(supervisor, monkeypatch):
    monkeypatch.setattr(runtime.time, "monotonic", lambda: 0)
    monkeypatch.setattr(supervisor, "observe", lambda: supervisor.exits.update(exporter=9))
    assert not supervisor.ready("exporter", 1)


@pytest.mark.parametrize(
    "failed,reason",
    [
        ("herdr", "herdr_startup_failure"),
        ("exporter", "exporter_startup_failure"),
        ("agent", "agent_startup_failure"),
        (None, None),
    ],
)
def test_start_handshakes_before_launching_agent(supervisor, monkeypatch, failed, reason):
    calls = []
    monkeypatch.setattr(runtime.time, "monotonic", lambda: 0)
    monkeypatch.setattr(supervisor, "spawn", lambda role, command: calls.append(("spawn", role, command)))
    monkeypatch.setattr(
        supervisor, "ready", lambda role, deadline: calls.append(("ready", role, deadline)) or role != failed
    )
    native = Mock(return_value=supervisor.launch.agent)
    monkeypatch.setattr(runtime, "native_command", native)
    herdr = Mock(return_value={"result": {"root_pane": {"pane_id": "pane"}}})
    monkeypatch.setattr(supervisor, "herdr", herdr)
    assert supervisor.start() == reason
    assert calls[:3] == [
        ("spawn", "herdr", ("herdr", "server")),
        ("spawn", "exporter", ("exporter",)),
        ("ready", "herdr", 2),
    ]
    if failed in ("herdr", "exporter"):
        herdr.assert_not_called()
    else:
        assert herdr.call_args_list[0].args[0] == [
            "workspace",
            "create",
            "--cwd",
            str(supervisor.launch.attempt),
            "--no-focus",
        ]
        assert herdr.call_args_list[1].args[0][:3] == ["pane", "run", "pane"]
        assert shlex.split(herdr.call_args_list[1].args[0][3]) == [
            sys.executable,
            "-m",
            "scripts.swarm_v2.supervision_agent",
            "tool",
        ]
        native.assert_called_once_with(supervisor.launch.agent, supervisor.launch.attempt, supervisor.environment)
        assert supervisor.phase == "agent_launch"
    assert (supervisor.root / "running.json").exists() == (failed is None)
    if failed is None:
        assert protocol.read(supervisor.root / "running.json") == {
            **supervisor.scope,
            "pane_id": "pane",
            "status": "running",
        }


def test_start_reports_termination_during_handshake(supervisor, monkeypatch):
    supervisor.stop = signal.SIGTERM
    monkeypatch.setattr(supervisor, "spawn", lambda *_: None)
    assert supervisor.start() == "termination"


@pytest.mark.parametrize(
    "role,code,expected",
    [
        ("agent", 0, "agent_completed"),
        ("agent", 7, "agent_failure"),
        ("herdr", 0, "herdr_failure"),
        ("exporter", 9, "exporter_failure"),
    ],
)
def test_running_classifies_child_exit(supervisor, monkeypatch, role, code, expected):
    wait = Mock(side_effect=[lambda: None])

    def observe_exit():
        wait()
        supervisor.exits[role] = code

    monkeypatch.setattr(supervisor, "wait", observe_exit)
    assert supervisor.running() == expected
    wait.assert_called_once_with()


def test_running_prioritizes_requested_termination(supervisor, monkeypatch):
    monkeypatch.setattr(supervisor, "wait", lambda: setattr(supervisor, "stop", signal.SIGTERM))
    assert supervisor.running() == "termination"


@pytest.mark.parametrize("forced", [False, True])
def test_quiesce_excludes_exporter_and_bounds_descendants(supervisor, monkeypatch, forced):
    supervisor.children["exporter"] = SimpleNamespace(pid=321)
    remaining = [SimpleNamespace(pid=123)]
    living = Mock(side_effect=[remaining, remaining if forced else [], []] if forced else [remaining, []])
    monkeypatch.setattr(runtime.trees, "living", living)
    send = Mock()
    monkeypatch.setattr(runtime.trees, "send", send)
    monkeypatch.setattr(supervisor, "wait", lambda: None)
    clock = iter([0, 0, 2, 2]) if forced else iter([0, 0])
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(clock))
    assert supervisor.quiesce() is (not forced)
    assert all(call.args == (os.getpid(), 321) for call in living.call_args_list)
    assert send.call_args_list[0].args == (remaining, signal.SIGTERM)
    assert send.call_count == (2 if forced else 1)
    if forced:
        assert send.call_args_list[1].args == (remaining, signal.SIGKILL)


@pytest.mark.parametrize("clean,identifier", [(True, "checkpoint"), (True, None), (False, None)])
def test_drain_reports_acknowledged_material_only(supervisor, monkeypatch, clean, identifier):
    monkeypatch.setattr(supervisor, "quiesce", lambda: clean)
    supervisor.stop = signal.SIGTERM
    supervisor.exits.update(agent=0)
    supervisor.reaped.add(321)
    protocol.write(supervisor.root / "agent.json", {**supervisor.scope, "pid": 123})
    acknowledgement = {**supervisor.scope, "status": "complete"}
    protocol.write(supervisor.root / "exporter.checkpoint.json", acknowledgement)
    checkpoint = Mock(return_value=identifier)
    monkeypatch.setattr(runtime, "checkpoint", checkpoint)
    cleanup = Mock()
    monkeypatch.setattr(runtime.trees, "cleanup", cleanup)
    clock = iter([0, 0, 2])
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(clock))
    wait = Mock()
    monkeypatch.setattr(supervisor, "wait", wait)
    result = supervisor.drain("termination")
    assert result["checkpoint_status"] == ("complete" if identifier else "incomplete")
    assert result["checkpoint_id"] == identifier
    assert result["quiescence"] == ("clean" if clean else "forced")
    assert result["signal"] == signal.SIGTERM
    assert result["child_exits"] == {"agent": 0}
    assert result["supervisor_child_exit_total"] == 2
    assert protocol.read(supervisor.root / "result.json") == result
    assert protocol.read(supervisor.root / "drain.json") == {
        **supervisor.scope,
        "reason": "termination",
        "status": "draining",
    }
    assert result["failure_class"] is None
    assert result["failure_stage"] is None
    assert protocol.read(supervisor.root / "quiesced.json")["status"] == ("quiesced" if clean else "forced")
    cleanup.assert_called_once_with(os.getpid(), 1, supervisor.observe)
    assert checkpoint.call_count == int(clean)
    if clean:
        checkpoint.assert_called_once_with(supervisor.launch.attempt, acknowledgement, supervisor.scope)
    assert wait.call_count == int(clean and not identifier)


@pytest.mark.parametrize(
    "reason,status,code",
    [("agent_completed", "complete", 0), ("termination", "incomplete", 75), ("exporter_failure", "complete", 70)],
)
def test_run_restores_handlers_and_returns_observed_outcome(supervisor, monkeypatch, capsys, reason, status, code):
    (supervisor.root / "context.json").unlink()
    supervisor.root.rmdir()
    supervisor.root.parent.rmdir()
    supervisor.root.parent.parent.rmdir()
    monkeypatch.setattr(runtime.trees, "subreaper", Mock())
    cleanup = Mock()
    monkeypatch.setattr(runtime.trees, "cleanup", cleanup)
    handlers = Mock(return_value="previous")
    monkeypatch.setattr(runtime.signal, "signal", handlers)
    monkeypatch.setattr(supervisor, "start", lambda: None)
    monkeypatch.setattr(supervisor, "running", lambda: reason)
    monkeypatch.setattr(supervisor, "drain", lambda observed: {"reason": observed, "checkpoint_status": status})
    printed = Mock(wraps=print)
    monkeypatch.setattr(runtime, "print", printed, raising=False)
    assert supervisor.run() == code
    printed.assert_called_once_with(
        json.dumps({"reason": reason, "checkpoint_status": status}, sort_keys=True), flush=True
    )
    assert protocol.read(supervisor.root / "context.json") == supervisor.scope
    assert (supervisor.root.parent / "owner.lock").exists()
    assert [call.args for call in handlers.call_args_list[:3]] == [
        (sig, supervisor.signal) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGCHLD)
    ]
    assert json.loads(capsys.readouterr().out) == {"reason": reason, "checkpoint_status": status}
    assert handlers.call_count == 6
    assert [call.args for call in handlers.call_args_list[3:]] == [
        (sig, "previous") for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGCHLD)
    ]
    cleanup.assert_called_once_with(os.getpid(), 1, supervisor.observe)


def test_run_records_sanitized_startup_failure(supervisor, monkeypatch, capsys):
    (supervisor.root / "context.json").unlink()
    supervisor.root.rmdir()
    monkeypatch.setattr(runtime.trees, "subreaper", lambda: None)
    monkeypatch.setattr(runtime.trees, "cleanup", lambda *_: None)
    monkeypatch.setattr(runtime.signal, "signal", lambda *_: None)
    monkeypatch.setattr(supervisor, "start", Mock(side_effect=ValueError("sensitive input")))
    monkeypatch.setattr(supervisor, "quiesce", lambda: False)
    assert supervisor.run() == 70
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == "supervisor_failure"
    assert result["failure_class"] == "ValueError"
    assert result["failure_stage"] == "startup"
    assert "sensitive input" not in json.dumps(result)


def test_main_refuses_invalid_arguments_and_launch(capsys):
    assert runtime.main([]) == 64
    assert runtime.main(["missing", "missing"]) == 64
    assert capsys.readouterr().err.splitlines() == [
        "ERROR supervisor requires attempt directory and launch specification",
        "ERROR supervisor launch refused",
    ]


@pytest.mark.parametrize("implicit", [False, True])
@pytest.mark.parametrize("code,expected", [(0, 0), (7, 7), (-signal.SIGTERM, 143)])
def test_agent_handshake_exit_and_forwarding(tmp_path, monkeypatch, code, expected, implicit):
    scope = {"incarnation": "current"}
    protocol.write(tmp_path / "context.json", scope)
    monkeypatch.setenv("SWARM_SUPERVISION_DIR", str(tmp_path))
    handlers = {}
    monkeypatch.setattr(agent.signal, "signal", lambda sig, handler: handlers.update({sig: handler}))
    child = Mock(pid=321)
    child.poll.return_value = None

    def finish():
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        child.poll.return_value = 0
        handlers[signal.SIGINT](signal.SIGINT, None)
        return code

    child.wait.side_effect = finish
    spawn = Mock(return_value=child)
    monkeypatch.setattr(agent.subprocess, "Popen", spawn)
    probe = Mock(return_value=SimpleNamespace(start_time=123))
    monkeypatch.setattr(agent, "_process", probe)
    monkeypatch.setattr(agent.sys, "argv", ["wrapper", "tool", "argument"])
    assert agent.main(None if implicit else ["tool", "argument"]) == expected
    spawn.assert_called_once_with(["tool", "argument"], stdin=None)
    child.send_signal.assert_called_once_with(signal.SIGTERM)
    probe.assert_called_once_with(os.getpid(), Path("/proc"))
    assert protocol.read(tmp_path / "agent.json") == {
        **scope,
        "pid": os.getpid(),
        "pid_start": 123,
        "child_pid": 321,
        "status": "ready",
    }
    assert protocol.read(tmp_path / "agent.exit.json") == {**scope, "exit_code": code}


def test_agent_refuses_launch_after_drain(tmp_path, monkeypatch):
    protocol.write(tmp_path / "context.json", {"incarnation": "current"})
    (tmp_path / "drain.json").touch()
    monkeypatch.setenv("SWARM_SUPERVISION_DIR", str(tmp_path))
    spawn = Mock()
    monkeypatch.setattr(agent.subprocess, "Popen", spawn)
    assert agent.main(["tool"]) == 75
    spawn.assert_not_called()
    assert protocol.read(tmp_path / "agent.exit.json")["exit_code"] == 75


@pytest.mark.parametrize("raw", [b"invalid", b"[]", b"null"])
def test_protocol_rejects_invalid_receipts(raw):
    assert protocol.decode(raw) == {}


def test_scope_and_runtime_environment_are_attempt_local(supervisor):
    assert supervisor.root.parent == supervisor.launch.attempt / "run" / "supervision"
    assert supervisor.scope == {
        "authority": supervisor.launch.authority,
        "incarnation": supervisor.root.name,
        "supervisor_pid": os.getpid(),
        "process_namespace": os.readlink("/proc/self/ns/pid"),
    }
    assert supervisor.environment["XDG_RUNTIME_DIR"] == str(supervisor.launch.attempt / "tmp")
    assert supervisor.environment["HERDR_CONFIG_PATH"] == str(supervisor.root / "herdr.toml")
    assert supervisor.environment["SWARM_SUPERVISION_DIR"] == str(supervisor.root)
    assert supervisor.failure_class is None


def test_herdr_command_preserves_prefix_environment_and_call_budget(supervisor, monkeypatch):
    launch = supervisor.launch
    supervisor.launch = Launch(
        launch.attempt,
        launch.authority,
        launch.home,
        launch.agent,
        launch.exporter,
        ("python", "fixture", "herdr", "server"),
        launch.budgets,
    )
    run = Mock(return_value=SimpleNamespace(stdout=b'{"result": {"ready": true}}'))
    monkeypatch.setattr(runtime.subprocess, "run", run)
    assert supervisor.herdr(["workspace", "list"]) == {"result": {"ready": True}}
    run.assert_called_once_with(
        ["python", "fixture", "herdr", "workspace", "list"],
        env=supervisor.environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=0.5,
        check=True,
    )


def test_ready_accepts_agent_handshake_even_after_agent_exit(supervisor, monkeypatch):
    supervisor.exits["agent"] = 0
    protocol.write(supervisor.root / "agent.json", {**supervisor.scope, "status": "ready"})
    monkeypatch.setattr(supervisor, "observe", lambda: None)
    monkeypatch.setattr(runtime.time, "monotonic", Mock(side_effect=[0]))
    assert supervisor.ready("agent", 1)


def test_exporter_readiness_cannot_mask_service_failure(supervisor, monkeypatch):
    supervisor.exits["herdr"] = 9
    protocol.write(supervisor.root / "exporter.json", {**supervisor.scope, "status": "ready"})
    monkeypatch.setattr(supervisor, "observe", lambda: None)
    monkeypatch.setattr(runtime.time, "monotonic", Mock(side_effect=[0]))
    assert not supervisor.ready("exporter", 1)


def test_ready_accepts_successful_herdr_transport(supervisor, monkeypatch):
    monkeypatch.setattr(supervisor, "observe", lambda: None)
    monkeypatch.setattr(runtime.time, "monotonic", Mock(side_effect=[0]))
    transport = Mock(return_value={})
    monkeypatch.setattr(supervisor, "herdr", transport)
    assert supervisor.ready("herdr", 1)
    transport.assert_called_once_with(["workspace", "list"])


def test_readiness_closes_at_deadline(supervisor, monkeypatch):
    monkeypatch.setattr(runtime.time, "monotonic", Mock(side_effect=[1]))
    observe = Mock()
    monkeypatch.setattr(supervisor, "observe", observe)
    assert not supervisor.ready("exporter", 1)
    observe.assert_not_called()


def test_agent_launch_termination_uses_the_drain_reason(supervisor, monkeypatch):
    monkeypatch.setattr(supervisor, "spawn", lambda *_: None)
    monkeypatch.setattr(supervisor, "herdr", lambda *_: {"result": {"root_pane": {"pane_id": "pane"}}})

    def ready(role, deadline):
        if role == "agent":
            supervisor.stop = signal.SIGTERM
            return False
        return True

    monkeypatch.setattr(supervisor, "ready", ready)
    assert supervisor.start() == "termination"


def test_workspace_failure_retains_its_diagnostic_stage(supervisor, monkeypatch):
    monkeypatch.setattr(supervisor, "spawn", lambda *_: None)
    monkeypatch.setattr(supervisor, "ready", lambda *_: True)
    monkeypatch.setattr(supervisor, "herdr", Mock(side_effect=ValueError("bad transport")))
    with pytest.raises(ValueError):
        supervisor.start()
    assert supervisor.phase == "workspace_create"


def test_observe_rejects_agent_from_stale_incarnation(supervisor, monkeypatch):
    monkeypatch.setattr(runtime.trees, "reap", lambda: [])
    probe = Mock()
    monkeypatch.setattr(runtime, "_process", probe)
    protocol.write(supervisor.root / "agent.json", {**supervisor.scope, "incarnation": "old", "pid": 123})
    supervisor.observe()
    probe.assert_not_called()
    assert supervisor.exits == {}


def test_quiescence_closes_both_windows_at_the_deadline(supervisor, monkeypatch):
    remaining = {123: SimpleNamespace(pid=123)}
    monkeypatch.setattr(runtime.trees, "living", lambda *_: remaining)
    send = Mock()
    monkeypatch.setattr(runtime.trees, "send", send)
    wait = Mock(side_effect=AssertionError("wait after deadline"))
    monkeypatch.setattr(supervisor, "wait", wait)
    monkeypatch.setattr(runtime.time, "monotonic", Mock(side_effect=[0, 1, 1, 2]))
    assert not supervisor.quiesce()
    wait.assert_not_called()
    assert [call.args for call in send.call_args_list] == [(remaining, signal.SIGTERM), (remaining, signal.SIGKILL)]


def test_checkpoint_window_closes_at_deadline(supervisor, monkeypatch):
    monkeypatch.setattr(supervisor, "quiesce", lambda: True)
    monkeypatch.setattr(runtime.trees, "cleanup", lambda *_: None)
    monkeypatch.setattr(runtime.time, "monotonic", Mock(side_effect=[0, 1]))
    checkpoint = Mock()
    monkeypatch.setattr(runtime, "checkpoint", checkpoint)
    assert supervisor.drain("termination")["checkpoint_status"] == "incomplete"
    checkpoint.assert_not_called()


def test_exporter_exit_ends_checkpoint_wait_and_stale_agent_is_not_counted(supervisor, monkeypatch):
    monkeypatch.setattr(supervisor, "quiesce", lambda: True)
    monkeypatch.setattr(runtime.trees, "cleanup", lambda *_: None)
    monkeypatch.setattr(runtime.time, "monotonic", Mock(side_effect=[0, 0]))
    monkeypatch.setattr(runtime, "checkpoint", lambda *_: None)
    supervisor.exits.update(exporter=9, agent=0)
    protocol.write(supervisor.root / "agent.json", {**supervisor.scope, "incarnation": "old", "pid": 123})
    wait = Mock(side_effect=AssertionError("wait after exporter exit"))
    monkeypatch.setattr(supervisor, "wait", wait)
    result = supervisor.drain("termination")
    assert result["checkpoint_status"] == "incomplete"
    assert result["supervisor_child_exit_total"] == 0
    wait.assert_not_called()


def test_run_refuses_an_existing_owner(supervisor):
    import fcntl

    with (supervisor.root.parent / "owner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(LaunchRefused, match="^execution already supervised$"):
            supervisor.run()


@pytest.mark.parametrize("implicit", [False, True])
def test_main_routes_the_admitted_paths(monkeypatch, tmp_path, implicit):
    attempt = tmp_path / "attempt"
    specification = tmp_path / "launch.json"
    load = Mock(return_value="launch")
    monkeypatch.setattr(runtime.Launch, "load", load)
    run = Mock(return_value=17)
    owner = Mock(return_value=Mock(run=run))
    monkeypatch.setattr(runtime, "Supervisor", owner)
    arguments = [str(attempt), str(specification)]
    monkeypatch.setattr(runtime.sys, "argv", ["supervisor", *arguments])
    assert runtime.main(None if implicit else arguments) == 17
    load.assert_called_once_with(attempt, specification)
    owner.assert_called_once_with("launch")
    run.assert_called_once_with()


def test_forced_quiescence_waits_for_the_kill_window(supervisor, monkeypatch):
    remaining = {123: SimpleNamespace(pid=123)}
    monkeypatch.setattr(runtime.trees, "living", Mock(side_effect=[remaining, remaining, []]))
    monkeypatch.setattr(runtime.trees, "send", lambda *_: None)
    monkeypatch.setattr(runtime.time, "monotonic", Mock(side_effect=[0, 1, 1, 1]))
    wait = Mock()
    monkeypatch.setattr(supervisor, "wait", wait)
    assert not supervisor.quiesce()
    wait.assert_called_once_with()
