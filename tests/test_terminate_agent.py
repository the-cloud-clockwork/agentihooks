import signal
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.terminate_agent import Process, Session, main, resolve, validate


def process(pid, *, ppid=1, pgid=None, sid=None, start=100, comm="claude", argv=()):
    group = pid if pgid is None else pgid
    return Process(pid, ppid, group, group if sid is None else sid, start, "S", comm, tuple(argv))


def session(session_id="uuid-1", name="engineer", item=None, target="claude"):
    item = item or process(200, pgid=190, sid=190, argv=("claude", "--name", name))
    return Session(session_id, target, name, item, "/repo", "alive")


def test_resolve_exact_name_uuid_and_pid():
    item = session()
    assert resolve([item], "engineer", "any") == item
    assert resolve([item], "uuid-1", "claude") == item
    assert resolve([item], "200", "claude") == item


def test_resolve_duplicate_name_requires_uuid():
    items = [session("uuid-1", item=process(200)), session("uuid-2", item=process(201))]
    with pytest.raises(ValueError, match="ambiguous"):
        resolve(items, "engineer", "claude")


def test_validate_rejects_shared_process_without_override(monkeypatch):
    item = session()
    other = session("uuid-2")
    table = {
        100: process(100, comm="python", argv=("python",)),
        190: process(190, pgid=190, sid=190, comm="bash", argv=("bash", "launcher")),
        200: item.process,
    }
    monkeypatch.setattr("scripts.terminate_agent.processes", lambda proc=Path("/proc"): table)
    monkeypatch.setattr("scripts.terminate_agent.os.getpid", lambda: 100)
    with pytest.raises(ValueError, match="shared"):
        validate(item, [item, other])


@pytest.mark.parametrize("state", ["Z", "X"])
def test_validate_leaves_out_group_members_that_ended(monkeypatch, state):
    item = session()
    ended = Process(201, 200, 190, 190, 100, state, "node", ("node",))
    table = {100: process(100, comm="python", argv=("python",)), 200: item.process, 201: ended}
    monkeypatch.setattr("scripts.terminate_agent.processes", lambda proc=Path("/proc"): table)
    monkeypatch.setattr("scripts.terminate_agent.os.getpid", lambda: 100)
    assert validate(item, [item]) == [item.process]


@pytest.mark.parametrize("state,alive", [("S", True), ("Z", False), ("X", False)])
def test_alive_counts_a_zombie_or_a_process_being_reaped_as_ended(monkeypatch, state, alive):
    from scripts.terminate_agent import _alive

    current = Process(7, 1, 7, 7, 100, state, "claude", ())
    monkeypatch.setattr("scripts.terminate_agent._process", lambda pid, proc: current)
    assert _alive({7: (100, "claude")}, Path("/proc")) == ([current] if alive else [])


def test_main_dry_run_sends_no_signal(monkeypatch, capsys):
    item = session(name="engineer-270926-2337-b")
    monkeypatch.setattr("scripts.terminate_agent.sessions", lambda: [item])
    monkeypatch.setattr(
        "scripts.terminate_agent.validate", lambda selected, items, force_shared=False: [selected.process]
    )
    with patch("scripts.terminate_agent.os.killpg") as killpg:
        assert main(["engineer-270926-2337-b", "--type", "claude", "--dry-run"]) == 0
    killpg.assert_not_called()
    assert "result=validated signal=none" in capsys.readouterr().out


def test_main_terminates_after_validation(monkeypatch, capsys):
    item = session()
    monkeypatch.setattr("scripts.terminate_agent.sessions", lambda: [item])
    monkeypatch.setattr(
        "scripts.terminate_agent.validate", lambda selected, items, force_shared=False: [selected.process]
    )
    monkeypatch.setattr("scripts.terminate_agent.terminate", lambda selected, members, timeout: True)
    assert main(["uuid-1", "--type", "claude"]) == 0
    assert "result=terminated escalation=SIGKILL" in capsys.readouterr().out


def test_terminate_uses_process_group(monkeypatch):
    from scripts.terminate_agent import terminate

    item = session()
    calls = []
    monkeypatch.setattr("scripts.terminate_agent._alive", lambda identities, proc: [])
    monkeypatch.setattr("scripts.terminate_agent.os.killpg", lambda pgid, sig: calls.append((pgid, sig)))
    assert terminate(item, [item.process], 0.1) is False
    assert calls == [(190, signal.SIGTERM)]


def test_terminate_waits_for_the_group_to_exit(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from scripts import terminate_agent

    item = session()
    alive = Mock(side_effect=[[item.process], [], [], []])
    monkeypatch.setattr(terminate_agent, "_alive", alive)
    monotonic = Mock(side_effect=[0.0, 0.0, 0.1])
    sleep = Mock()
    monkeypatch.setattr(terminate_agent, "time", SimpleNamespace(monotonic=monotonic, sleep=sleep))
    kill = Mock()
    monkeypatch.setattr(terminate_agent.os, "killpg", kill)

    assert terminate_agent.terminate(item, [item.process], 1) is False

    sleep.assert_called_once_with(0.1)
    kill.assert_called_once_with(item.process.pgid, signal.SIGTERM)
    assert alive.call_count == 4


def test_terminate_real_isolated_process_group():
    from scripts.terminate_agent import _process, terminate

    child = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        time.sleep(0.05)
        item = _process(child.pid, Path("/proc"))
        assert item is not None
        target = Session("probe", "claude", "probe", item, "", "alive")
        assert terminate(target, [item], 0.5) is False
        assert child.wait(timeout=1) == -signal.SIGTERM
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def _herdr_env(tmp_path, pid: int, **env) -> Path:
    proc = tmp_path / "proc"
    (proc / str(pid)).mkdir(parents=True)
    raw = b"\0".join(f"{k}={v}".encode() for k, v in {"SECRET_TOKEN": "x", **env}.items()) + b"\0"
    (proc / str(pid) / "environ").write_bytes(raw)
    return proc


def test_the_herdr_pane_is_read_from_the_agent_environment(tmp_path):
    from scripts.terminate_agent import herdr_pane

    proc = _herdr_env(tmp_path, 200, HERDR_PANE_ID="w1:p7", HERDR_SOCKET_PATH="/s/herdr.sock")
    assert herdr_pane(200, proc) == ("w1:p7", "/s/herdr.sock")
    assert herdr_pane(201, proc) == ("", "")


def _terminate_in_herdr(monkeypatch, *extra):
    item = session()
    closed = []
    monkeypatch.setattr("scripts.terminate_agent.sessions", lambda: [item])
    monkeypatch.setattr(
        "scripts.terminate_agent.validate", lambda selected, items, force_shared=False: [selected.process]
    )
    monkeypatch.setattr("scripts.terminate_agent.terminate", lambda selected, members, timeout: False)
    monkeypatch.setattr("scripts.terminate_agent.herdr_pane", lambda pid: ("w1:p7", "/s/herdr.sock"))
    monkeypatch.setattr(
        "scripts.herdr_host._cli", lambda args, environ: closed.append((args, environ["HERDR_SOCKET_PATH"])) or {}
    )
    return main(["uuid-1", "--type", "claude", *extra]), closed


def test_terminating_an_agent_in_herdr_closes_its_pane(monkeypatch, capsys):
    rc, closed = _terminate_in_herdr(monkeypatch)
    out = capsys.readouterr().out
    assert rc == 0
    assert closed == [(["pane", "close", "w1:p7"], "/s/herdr.sock")]
    assert "pane_id=w1:p7" in out and "pane=closed" in out


@pytest.mark.parametrize("recorded_socket", [None, "", "/s/target.sock"])
def test_termination_does_not_close_a_matching_pane_on_the_caller_server(
    monkeypatch, tmp_path, capsys, recorded_socket
):
    from scripts import herdr_host, terminate_agent

    item = session()
    pane = "w1:p7"
    default_socket = str(Path.home() / ".config" / "herdr" / "herdr.sock")
    caller_socket = "/s/caller.sock"
    target_socket = recorded_socket or default_socket
    panes = {target_socket: {pane}, caller_socket: {pane}}
    target_env = {"HERDR_PANE_ID": pane}
    if recorded_socket is not None:
        target_env["HERDR_SOCKET_PATH"] = recorded_socket
    proc = _herdr_env(tmp_path, item.process.pid, **target_env)
    read_pane = terminate_agent.herdr_pane
    monkeypatch.setenv("HERDR_SOCKET_PATH", caller_socket)
    monkeypatch.setattr(terminate_agent, "sessions", lambda: [item])
    monkeypatch.setattr(terminate_agent, "validate", lambda selected, items, force_shared=False: [selected.process])
    monkeypatch.setattr(terminate_agent, "terminate", lambda selected, members, timeout: False)
    monkeypatch.setattr(terminate_agent, "herdr_pane", lambda pid: read_pane(pid, proc))
    monkeypatch.setattr(herdr_host, "binary", lambda: "herdr-test")

    def run(args, *, capture_output, text, env, timeout):
        assert args == ["herdr-test", "pane", "close", pane]
        endpoint = env.get("HERDR_SOCKET_PATH") or default_socket
        panes[endpoint].remove(pane)
        return subprocess.CompletedProcess(args, 0, stdout='{"result": {}}', stderr="")

    monkeypatch.setattr(herdr_host.subprocess, "run", run)
    assert main([item.session_id, "--type", "claude"]) == 0
    assert panes[target_socket] == set()
    assert panes[caller_socket] == {pane}
    assert capsys.readouterr().out.endswith("result=terminated escalation=none\npane_id=w1:p7 pane=closed\n")


def test_keep_pane_leaves_the_herdr_pane_open(monkeypatch, capsys):
    rc, closed = _terminate_in_herdr(monkeypatch, "--keep-pane")
    assert rc == 0 and closed == []
    assert "pane=kept" in capsys.readouterr().out


def test_a_pane_herdr_already_closed_counts_as_closed(monkeypatch, capsys):
    from scripts import herdr_host

    item = session()
    monkeypatch.setattr("scripts.terminate_agent.sessions", lambda: [item])
    monkeypatch.setattr(
        "scripts.terminate_agent.validate", lambda selected, items, force_shared=False: [selected.process]
    )
    monkeypatch.setattr("scripts.terminate_agent.terminate", lambda selected, members, timeout: False)
    monkeypatch.setattr("scripts.terminate_agent.herdr_pane", lambda pid: ("w1:p7", ""))

    def gone(args, environ):
        raise herdr_host.HerdrError("herdr pane close: pane w1:p7 not found")

    monkeypatch.setattr("scripts.herdr_host._cli", gone)
    assert main(["uuid-1", "--type", "claude"]) == 0
    assert "pane_id=w1:p7 pane=closed" in capsys.readouterr().out


def test_an_agent_without_a_name_argument_is_named_from_its_environment(tmp_path):
    from scripts.terminate_agent import sessions

    item = process(300, comm="codex", argv=("/bin/codex",))
    proc = _herdr_env(tmp_path, 300, AGENTIHOOKS_AGENT_NAME="smoke-codex")
    with patch("scripts.terminate_agent.processes", return_value={300: item}):
        found = sessions(proc, registry={})
    assert [(s.target, s.name) for s in found] == [("codex", "smoke-codex")]
    assert resolve(found, "smoke-codex", "codex").process.pid == 300


def test_a_child_agent_process_does_not_inherit_its_parents_name(tmp_path):
    from scripts.terminate_agent import sessions

    parent = process(300, comm="codex", argv=("/bin/codex",))
    child = process(301, ppid=300, comm="codex-code-mode", argv=("codex-code-mode",))
    proc = _herdr_env(tmp_path, 300, AGENTIHOOKS_AGENT_NAME="smoke-codex")
    (proc / "301").mkdir()
    (proc / "301" / "environ").write_bytes((proc / "300" / "environ").read_bytes())
    with patch("scripts.terminate_agent.processes", return_value={300: parent, 301: child}):
        found = sessions(proc, registry={})
    assert resolve(found, "smoke-codex", "codex").process.pid == 300


def _codex_registry(memories):
    return {
        "main-thread": {
            "status": "superseded",
            "pid": 300,
            "cwd": "/work/repo",
            "started_at": "2026-10-05T03:40:00Z",
        },
        "memory-helper": {
            "status": "alive",
            "pid": 300,
            "cwd": str(memories),
            "started_at": "2026-10-05T03:40:05Z",
        },
    }


def test_a_codex_session_lists_under_its_main_thread_not_its_memory_helper(tmp_path, monkeypatch):
    from scripts.terminate_agent import sessions

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    memories = tmp_path / "codex" / "memories"
    item = process(300, comm="codex", argv=("/bin/codex",))
    proc = _herdr_env(tmp_path, 300, AGENTIHOOKS_AGENT_NAME="m5-codex")
    with patch("scripts.terminate_agent.processes", return_value={300: item}):
        found = sessions(proc, registry=_codex_registry(memories))
    assert [(s.target, s.name, s.session_id, s.cwd, s.status) for s in found] == [
        ("codex", "m5-codex", "main-thread", "/work/repo", "alive")
    ]
    assert resolve(found, "m5-codex", "codex").process.pgid == 300


def test_a_codex_memory_helper_without_a_main_thread_record_lists_as_itself(tmp_path, monkeypatch):
    from scripts.terminate_agent import sessions

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    memories = tmp_path / "codex" / "memories"
    registry = _codex_registry(memories)
    del registry["main-thread"]
    item = process(300, comm="codex", argv=("/bin/codex",))
    with patch("scripts.terminate_agent.processes", return_value={300: item}):
        found = sessions(tmp_path / "proc", registry=registry)
    assert [(s.session_id, s.cwd) for s in found] == [("memory-helper", str(memories))]


def test_a_registered_codex_main_thread_lists_from_its_own_record(tmp_path, monkeypatch):
    from scripts.terminate_agent import sessions

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    registry = _codex_registry(tmp_path / "codex" / "memories")
    del registry["memory-helper"]
    registry["main-thread"]["status"] = "alive"
    item = process(300, comm="codex", argv=("/bin/codex",))
    with patch("scripts.terminate_agent.processes", return_value={300: item}):
        found = sessions(tmp_path / "proc", registry=registry)
    assert [(s.session_id, s.cwd) for s in found] == [("main-thread", "/work/repo")]


def test_claude_sessions_list_from_their_own_records(tmp_path, monkeypatch):
    from scripts.terminate_agent import sessions

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    item = process(400, argv=("claude", "--name", "engineer"))
    registry = {
        "old": {"status": "superseded", "pid": 400, "cwd": "/old", "started_at": "2026-10-05T03:00:00Z"},
        "uuid-1": {"status": "alive", "pid": 400, "cwd": "/repo", "started_at": "2026-10-05T04:00:00Z"},
    }
    with patch("scripts.terminate_agent.processes", return_value={400: item}):
        found = sessions(tmp_path / "proc", registry=registry)
    assert [(s.target, s.name, s.session_id, s.cwd, s.status) for s in found] == [
        ("claude", "engineer", "uuid-1", "/repo", "alive")
    ]


def test_sessions_name_a_registered_session_from_its_record(monkeypatch):
    from scripts.terminate_agent import sessions

    table = {300: process(300, comm="claude", argv=("claude",))}
    monkeypatch.setattr("scripts.terminate_agent.processes", lambda proc=Path("/proc"): table)
    monkeypatch.setattr("scripts.terminate_agent.agent_environ", lambda pid, keys, proc=None: ("",))
    registry = {"uuid-9": {"status": "alive", "pid": 300, "cwd": "/repo", "name": "sw-master-1"}}
    assert [s.name for s in sessions(registry=registry)] == ["sw-master-1"]


@pytest.mark.parametrize("argv", [("claude", "--name", "old-master"), ("codex",)])
def test_a_renamed_session_lists_under_its_registered_name(monkeypatch, argv):
    from scripts.terminate_agent import sessions

    table = {300: process(300, comm=argv[0], argv=argv)}
    monkeypatch.setattr("scripts.terminate_agent.processes", lambda proc: table)
    monkeypatch.setattr("scripts.terminate_agent.agent_environ", lambda pid, keys, proc: ("old-master",))
    registry = {"uuid-9": {"status": "alive", "pid": 300, "cwd": "/repo", "name": "master@a1b2c3-0001"}}
    assert [s.name for s in sessions(registry=registry)] == ["master@a1b2c3-0001"]
