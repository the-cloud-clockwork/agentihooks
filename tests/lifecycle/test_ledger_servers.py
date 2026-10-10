import json
import os
from pathlib import Path
from unittest.mock import Mock, call

import pytest

from hooks.lifecycle import ledger_servers
from hooks.proc import Process


def process(pid=42, ppid=7, start=100):
    return Process(pid, ppid, pid, pid, start, "S", "python", ("python", "ledger_server.py", "--serve"))


def plant(tmp_path, row, folder, port=9000, owner=None, cwd=None):
    proc = tmp_path / "proc"
    item = proc / str(row.pid)
    item.mkdir(parents=True)
    (item / "cwd").symlink_to(cwd or folder)
    env = {"LEDGER_DIR": str(folder), "LEDGER_PORT": str(port)}
    if owner:
        env.update(LEDGER_RUN_PID=str(owner[0]), LEDGER_RUN_START=str(owner[1]))
    (item / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()))
    (proc / "uptime").write_text("3600 0")
    return proc


@pytest.fixture
def pidfd(monkeypatch):
    handles = []

    def opened(pid):
        handles.append(os.open(os.devnull, os.O_RDONLY))
        return handles[-1]

    sent = Mock()
    monkeypatch.setattr(ledger_servers.os, "pidfd_open", opened)
    monkeypatch.setattr(ledger_servers.signal, "pidfd_send_signal", sent)
    monkeypatch.setattr(ledger_servers.os, "kill", Mock(side_effect=AssertionError("signal by pid")))
    return handles, sent


@pytest.mark.parametrize("reason", ["folder", "cwd", "owner", "legacy", "reused", "zombie"])
def test_sweep_stops_orphan_servers_and_logs_their_identity(tmp_path, monkeypatch, reason):
    folder = tmp_path / "ledger"
    folder.mkdir()
    row = process(ppid=1 if reason == "legacy" else 7)
    cwd = tmp_path / "missing" if reason == "cwd" else folder
    identity = None if reason == "legacy" else (7, 99)
    proc = plant(tmp_path, row, folder, owner=identity, cwd=cwd)
    parent = Process(7, 1, 7, 7, 99, "S", "pytest", ("pytest",))
    if reason == "folder":
        folder.rmdir()
    if reason == "reused":
        parent = Process(7, 1, 7, 7, 101, "S", "pytest", ("pytest",))
    if reason == "zombie":
        parent = Process(7, 1, 7, 7, 99, "Z", "pytest", ("pytest",))
    table = {42: row, **({7: parent} if reason not in {"owner", "legacy"} else {})}
    stopped = []
    monkeypatch.setattr(ledger_servers, "terminate", lambda row, proc: stopped.append(row.pid))
    home = tmp_path / "state"
    home.mkdir()
    result = ledger_servers.sweep_servers(table, home, act=True, proc=proc)
    assert stopped == [42]
    assert len(result) == 1
    assert result[0]["pid"] == 42
    assert result[0]["port"] == 9000
    assert result[0]["folder"] == str(folder)
    assert result[0]["age"] == 3600 - 100 / os.sysconf("SC_CLK_TCK")
    assert result[0]["action"] == "stopped"
    assert result[0]["reason"] == (
        "working folder is gone"
        if reason in {"folder", "cwd"}
        else "starting run ended without an owner record"
        if reason == "legacy"
        else "starting run ended"
    )
    assert json.loads((home / "gc-ledger-servers.jsonl").read_text()) == result[0]


@pytest.mark.parametrize("vanished_at", ["identity", "sigterm", "sigkill"])
def test_sweep_counts_a_server_that_vanishes_before_stopping_as_stopped(tmp_path, monkeypatch, pidfd, vanished_at):
    import signal

    handles, kill = pidfd
    folder = tmp_path / "ledger"
    folder.mkdir()
    row = process()
    proc = plant(tmp_path, row, folder, owner=(7, 99))
    monkeypatch.setattr(
        ledger_servers, "_process", Mock(side_effect=[None] if vanished_at == "identity" else [row, row])
    )
    missing = ProcessLookupError(3, "No such process")
    kill.side_effect = [None, missing] if vanished_at == "sigkill" else missing
    monkeypatch.setattr(ledger_servers.time, "monotonic", Mock(side_effect=[1, 2]))

    result = ledger_servers.sweep_servers({row.pid: row}, tmp_path, act=True, proc=proc)

    assert len(result) == 1
    assert result[0]["action"] == "stopped"
    assert result[0]["pid"] == 42
    assert result[0]["reason"] == "starting run ended"
    assert json.loads((tmp_path / "gc-ledger-servers.jsonl").read_text()) == result[0]
    assert (
        kill.call_args_list
        == {
            "identity": [],
            "sigterm": [call(handles[0], signal.SIGTERM)],
            "sigkill": [call(handles[0], signal.SIGTERM), call(handles[0], signal.SIGKILL)],
        }[vanished_at]
    )


@pytest.mark.parametrize("failed_signal", ["sigterm", "sigkill"])
def test_sweep_reports_signal_permission_errors(tmp_path, monkeypatch, pidfd, failed_signal):
    _, kill = pidfd
    folder = tmp_path / "ledger"
    folder.mkdir()
    row = process()
    proc = plant(tmp_path, row, folder, owner=(7, 99))
    monkeypatch.setattr(ledger_servers, "_process", Mock(side_effect=[row, row]))
    error = PermissionError(1, "Operation not permitted")
    kill.side_effect = [None, error] if failed_signal == "sigkill" else error
    monkeypatch.setattr(ledger_servers.time, "monotonic", Mock(side_effect=[1, 2]))

    result = ledger_servers.sweep_servers({row.pid: row}, tmp_path, act=True, proc=proc)

    assert result == [{"pid": 42, "action": "error", "error": "[Errno 1] Operation not permitted"}]
    assert not (tmp_path / "gc-ledger-servers.jsonl").exists()


@pytest.mark.parametrize("kind", ["shared", "shared stale port", "live", "unrelated", "dry", "scope"])
def test_sweep_preserves_shared_active_and_unmatched_processes(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    folder = tmp_path / ("development-ledger" if kind.startswith("shared") else "ledger")
    folder.mkdir()
    row = process(ppid=1 if kind.startswith("shared") else 7)
    if kind == "unrelated":
        row = Process(42, 7, 42, 42, 100, "S", "python", ("python", "other.py", "--serve"))
    proc = plant(tmp_path, row, folder, port=8765 if kind == "shared" else 9000, owner=(7, 99))
    parent = Process(7, 1, 7, 7, 99, "S", "pytest", ("pytest",))
    table = {42: row, **({7: parent} if kind == "live" else {})}
    monkeypatch.setattr(ledger_servers, "terminate", lambda *a: pytest.fail("must not terminate"))
    home = tmp_path / "state"
    home.mkdir()
    result = ledger_servers.sweep_servers(
        table, home, scope=str(tmp_path / "other") if kind == "scope" else "", act=kind != "dry", proc=proc
    )
    assert result[0]["action"] == "would stop" if kind == "dry" else result == []
    assert not (home / "gc-ledger-servers.jsonl").exists()


@pytest.mark.parametrize(
    "argv,expected",
    [
        (("python", "-m", "scripts.swarm_ledger.ledger_server", "--serve"), True),
        (("python", "ledger_server.py", "--ensure"), False),
        (("python", "ledger_server.py", "--serve"), True),
        (("python", "other.py", "--serve"), False),
        (("python", "other.py", "ledger_server.py", "--serve"), False),
    ],
)
def test_only_ledger_serving_processes_are_candidates(argv, expected):
    row = Process(42, 7, 42, 42, 100, "S", "python", argv)
    assert ledger_servers.server_process(row) is expected


@pytest.mark.parametrize("folder_setting", ["default", "relative", "user", "equals"])
def test_details_resolve_the_server_folder_and_default_port(tmp_path, monkeypatch, folder_setting):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cwd = tmp_path / "run"
    cwd.mkdir()
    folder = {
        "default": tmp_path / "development-ledger",
        "relative": cwd / "data",
        "user": tmp_path / "data",
        "equals": tmp_path / "data=one",
    }[folder_setting]
    folder.mkdir()
    row = process(start=400000)
    proc = plant(tmp_path, row, folder, cwd=cwd)
    env = {
        "relative": b"LEDGER_DIR=data",
        "user": b"LEDGER_DIR=~/data",
        "equals": f"LEDGER_DIR={folder}".encode(),
        "default": b"",
    }[folder_setting]
    (proc / "42/environ").write_bytes(env)
    info = ledger_servers.details(row, proc)
    assert info == {"pid": 42, "port": 8765, "folder": str(folder), "cwd": str(cwd), "age": 0, "owner": None}


def test_sweep_checks_every_process_after_skips_and_errors(tmp_path, monkeypatch):
    folder = tmp_path / "ledger"
    folder.mkdir()
    cwd = tmp_path / "run"
    cwd.mkdir()
    row = process(ppid=1)
    proc = plant(tmp_path, row, folder, cwd=cwd)
    other = Process(1, 0, 1, 1, 0, "S", "unrelated", ("other.py",))
    active = process(pid=43)
    outside = process(pid=44, ppid=1)
    broken = process(pid=45)
    metadata = ledger_servers.details(row, proc)
    read = Mock(
        side_effect=[
            {**metadata, "pid": 43, "owner": [7, 99]},
            {**metadata, "pid": 44, "folder": "/outside", "cwd": "/outside"},
            OSError("unreadable"),
            metadata,
        ]
    )
    monkeypatch.setattr(ledger_servers, "details", read)
    stopped = Mock()
    monkeypatch.setattr(ledger_servers, "terminate", stopped)
    parent = Process(7, 1, 7, 7, 99, "S", "pytest", ("pytest",))
    table = {1: other, 43: active, 44: outside, 45: broken, 42: row, 7: parent}
    result = ledger_servers.sweep_servers(table, tmp_path, scope=str(tmp_path), act=True, proc=proc)
    assert result[0] == {"pid": 45, "action": "error", "error": "unreadable"}
    assert result[1]["pid"] == 42
    assert result[1]["reason"] == "starting run ended without an owner record"
    stopped.assert_called_once_with(row, proc)


def test_relative_shared_folder_still_uses_the_fixed_port(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    folder = tmp_path / "development-ledger"
    folder.mkdir()
    row = process(ppid=1)
    proc = plant(tmp_path, row, folder, cwd=tmp_path)
    (proc / "42/environ").write_bytes(b"LEDGER_DIR=development-ledger\0LEDGER_PORT=9999")
    info = ledger_servers.details(row, proc)
    assert info["folder"] == str(folder)
    assert info["port"] == 8765
    assert ledger_servers.orphan(row, info, {42: row}) == ""


def test_sweep_defaults_to_a_report_without_signalling_or_logging(tmp_path, monkeypatch):
    folder = tmp_path / "ledger"
    folder.mkdir()
    row = process(ppid=1)
    proc = plant(tmp_path, row, folder)
    stopped = Mock()
    monkeypatch.setattr(ledger_servers, "terminate", stopped)
    result = ledger_servers.sweep_servers({42: row}, tmp_path, proc=proc)
    assert result[0]["action"] == "would stop"
    assert result[0]["reason"] == "starting run ended without an owner record"
    stopped.assert_not_called()
    assert not (tmp_path / "gc-ledger-servers.jsonl").exists()


def test_terminate_allows_a_grace_period_before_escalating(tmp_path, monkeypatch, pidfd):
    handles, sent = pidfd
    row = process()
    monkeypatch.setattr(ledger_servers, "_process", Mock(side_effect=[row, row, None]))
    monkeypatch.setattr(ledger_servers.time, "monotonic", Mock(side_effect=[1, 1.5]))
    sleep = Mock()
    monkeypatch.setattr(ledger_servers.time, "sleep", sleep)
    ledger_servers.terminate(row, tmp_path)
    import signal

    sent.assert_called_once_with(handles[0], signal.SIGTERM)
    sleep.assert_called_once_with(0.02)


def test_foreground_owner_metadata_is_used_when_environment_has_no_start_time(tmp_path):
    folder = tmp_path / "ledger"
    folder.mkdir()
    row = process()
    proc = plant(tmp_path, row, folder)
    (folder / ".server.owner.json").write_text('{"pid":7,"start":99}')
    info = ledger_servers.details(row, proc)
    assert info["owner"] == [7, 99]
    assert info["cwd"] == str(folder)
    assert info["port"] == 9000


def test_unreadable_servers_are_reported_without_stopping_other_candidates(tmp_path, monkeypatch):
    home = tmp_path / "state"
    home.mkdir()
    row = process()
    stopped = Mock()
    monkeypatch.setattr(ledger_servers, "terminate", stopped)
    result = ledger_servers.sweep_servers({42: row}, home, act=True, proc=tmp_path)
    assert result == [
        {
            "pid": 42,
            "action": "error",
            "error": str(FileNotFoundError(2, "No such file or directory", str(tmp_path / "42/environ"))),
        }
    ]
    stopped.assert_not_called()


@pytest.mark.parametrize("outcome", ["gone", "zombie", "reused", "stuck"])
def test_terminate_signals_only_the_observed_process(tmp_path, monkeypatch, pidfd, outcome):
    handles, sent = pidfd
    row = process()
    gone = {
        "gone": None,
        "zombie": Process(42, 7, 42, 42, 100, "Z", "python", row.argv),
        "reused": process(start=101),
        "stuck": row,
    }[outcome]
    read = Mock(side_effect=[row, gone])
    monkeypatch.setattr(ledger_servers, "_process", read)
    monkeypatch.setattr(ledger_servers.time, "monotonic", Mock(side_effect=[1, 2]))
    ledger_servers.terminate(row, tmp_path)
    import signal

    assert sent.call_args_list == [call(handles[0], signal.SIGTERM)] + (
        [call(handles[0], signal.SIGKILL)] if outcome == "stuck" else []
    )
    assert read.call_args_list == [call(42, tmp_path), call(42, tmp_path)]


@pytest.mark.parametrize("current", [process(start=101), process(start=99)])
def test_terminate_refuses_a_changed_process_identity(tmp_path, monkeypatch, pidfd, current):
    _, sent = pidfd
    monkeypatch.setattr(ledger_servers, "_process", lambda pid, proc: current)
    with pytest.raises(ProcessLookupError, match="^server identity changed$"):
        ledger_servers.terminate(process(), tmp_path)
    sent.assert_not_called()


def test_terminate_stops_a_server_whose_state_changed_since_the_snapshot(tmp_path, monkeypatch, pidfd):
    handles, sent = pidfd
    row = Process(42, 7, 42, 42, 100, "R", "python", ("python", "ledger_server.py", "--serve"))
    monkeypatch.setattr(ledger_servers, "_process", Mock(side_effect=[process(), None]))
    monkeypatch.setattr(ledger_servers.time, "monotonic", Mock(side_effect=[1, 1.5]))
    ledger_servers.terminate(row, tmp_path)
    import signal

    sent.assert_called_once_with(handles[0], signal.SIGTERM)


def test_terminate_pins_the_process_before_checking_its_identity(tmp_path, monkeypatch, pidfd):
    handles, sent = pidfd
    row = process()
    steps = []

    def read(pid, proc):
        steps.append(("read", len(handles)))
        return row if len(steps) == 1 else None

    monkeypatch.setattr(ledger_servers, "_process", read)
    monkeypatch.setattr(ledger_servers.time, "monotonic", Mock(side_effect=[1, 1.5]))
    ledger_servers.terminate(row, tmp_path)

    assert steps == [("read", 1), ("read", 1)]
    assert len(sent.call_args_list) == 1
    with pytest.raises(OSError):
        os.fstat(handles[0])


def test_terminate_skips_a_server_that_exited_before_it_was_pinned(tmp_path, monkeypatch, pidfd):
    _, sent = pidfd
    monkeypatch.setattr(ledger_servers.os, "pidfd_open", Mock(side_effect=ProcessLookupError))
    read = Mock()
    monkeypatch.setattr(ledger_servers, "_process", read)
    ledger_servers.terminate(process(), tmp_path)
    read.assert_not_called()
    sent.assert_not_called()


def test_scope_accepts_the_working_folder_even_when_data_is_elsewhere(tmp_path, monkeypatch):
    folder = tmp_path / "ledger"
    cwd = tmp_path / "run"
    folder.mkdir()
    cwd.mkdir()
    row = process(ppid=1)
    proc = plant(tmp_path, row, folder, cwd=cwd)
    stopped = Mock()
    monkeypatch.setattr(ledger_servers, "terminate", stopped)
    result = ledger_servers.sweep_servers({42: row}, tmp_path, scope=str(cwd), act=True, proc=proc)
    assert result[0]["action"] == "stopped"
    stopped.assert_called_once_with(row, proc)


def test_sweep_really_terminates_a_server_from_a_deleted_working_folder(tmp_path, ledger_port):
    import shutil
    import subprocess
    import sys
    import time

    from hooks.proc import _process

    root = Path(__file__).resolve().parents[2]
    cwd = tmp_path / "run"
    folder = tmp_path / "data"
    cwd.mkdir()
    folder.mkdir()
    script = cwd / "ledger_server.py"
    script.write_text(
        "import threading\n"
        "from scripts.swarm_ledger import ledger_server, server_lifetime\n"
        "server_lifetime.watch = lambda *args: threading.Event()\n"
        "ledger_server.serve()\n"
    )
    port = ledger_port
    env = {
        **os.environ,
        "LEDGER_DIR": str(folder),
        "LEDGER_PORT": str(port),
        "PYTHONPATH": str(root),
        "SWARM_RELOAD": "0",
    }
    server = subprocess.Popen([sys.executable, str(script), "--serve"], cwd=cwd, env=env)
    try:
        deadline = time.monotonic() + 5
        while not (folder / ".server.pid").exists():
            assert server.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        row = _process(server.pid, Path("/proc"))
        assert row is not None
        shutil.rmtree(cwd)
        result = ledger_servers.sweep_servers({server.pid: row}, tmp_path, act=True)
        assert result[0]["action"] == "stopped"
        assert result[0]["pid"] == server.pid
        assert result[0]["port"] == port
        assert result[0]["folder"] == str(folder)
        assert result[0]["reason"] == "working folder is gone"
        assert server.wait(timeout=5) < 0
        assert json.loads((tmp_path / "gc-ledger-servers.jsonl").read_text()) == result[0]
    finally:
        if server.poll() is None:
            server.kill()
            server.wait(timeout=5)
