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
    assert json.loads((home / "gc-ledger-servers.jsonl").read_text()) == result[0]


@pytest.mark.parametrize("kind", ["shared", "live", "unrelated", "dry", "scope"])
def test_sweep_preserves_shared_active_and_unmatched_processes(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    folder = tmp_path / ("development-ledger" if kind == "shared" else "ledger")
    folder.mkdir()
    row = process(ppid=1 if kind == "shared" else 7)
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
def test_terminate_signals_only_the_observed_process(tmp_path, monkeypatch, outcome):
    row = process()
    gone = {
        "gone": None,
        "zombie": Process(42, 7, 42, 42, 100, "Z", "python", row.argv),
        "reused": process(start=101),
        "stuck": row,
    }[outcome]
    read = Mock(side_effect=[row, gone])
    kill = Mock()
    monkeypatch.setattr(ledger_servers, "_process", read)
    monkeypatch.setattr(ledger_servers.os, "kill", kill)
    monkeypatch.setattr(ledger_servers.time, "monotonic", Mock(side_effect=[1, 2]))
    ledger_servers.terminate(row, tmp_path)
    import signal

    assert kill.call_args_list == [call(42, signal.SIGTERM)] + (
        [call(42, signal.SIGKILL)] if outcome == "stuck" else []
    )
    assert read.call_args_list == [call(42, tmp_path), call(42, tmp_path)]


def test_terminate_refuses_a_changed_process_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_servers, "_process", lambda pid, proc: process(start=101))
    kill = Mock()
    monkeypatch.setattr(ledger_servers.os, "kill", kill)
    with pytest.raises(ProcessLookupError, match="^server identity changed$"):
        ledger_servers.terminate(process(), tmp_path)
    kill.assert_not_called()


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
