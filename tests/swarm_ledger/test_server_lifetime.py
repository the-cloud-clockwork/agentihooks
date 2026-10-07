import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.swarm_ledger import server_lifetime

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "scripts/swarm_ledger/ledger_server.py"


def running(pid):
    try:
        return (Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]) != "Z"
    except FileNotFoundError:
        return False


@pytest.mark.parametrize("ending", ["exit", "terminate", "kill"])
@pytest.mark.parametrize("explicit_owner", [True, False])
@pytest.mark.parametrize("mode", ["--ensure", "--serve"])
def test_detached_server_stops_when_its_run_ends(tmp_path, ending, explicit_owner, mode):
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        port = spare.getsockname()[1]
    env = {
        **os.environ,
        "LEDGER_DIR": str(tmp_path),
        "LEDGER_PORT": str(port),
        "SWARM_RELOAD": "0",
        "PYTHONPATH": str(ROOT),
    }
    env.pop("LEDGER_RUN_PID", None)
    env.pop("LEDGER_RUN_START", None)
    script = (
        "import os, subprocess, sys, time\n"
        + ("os.environ['LEDGER_RUN_PID'] = str(os.getpid())\n" if explicit_owner else "")
        + "if sys.argv[3] == '--ensure': subprocess.run([sys.executable, sys.argv[1], sys.argv[3]], check=True)\n"
        + "else: subprocess.Popen([sys.executable, sys.argv[1], sys.argv[3]])\n"
        "while not os.path.exists(sys.argv[2]): time.sleep(0.01)\n"
    )
    run = subprocess.Popen([sys.executable, "-c", script, str(SERVER), str(tmp_path / "end"), mode], env=env)
    pid = None
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / ".server.pid").exists():
            assert run.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        pid = int((tmp_path / ".server.pid").read_text())
        assert running(pid)
        if ending == "exit":
            (tmp_path / "end").touch()
        else:
            getattr(run, ending)()
        run.wait(timeout=5)
        deadline = time.monotonic() + 3
        while running(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not running(pid), f"ledger server {pid} on port {port} survived its ended run"
    finally:
        if run.poll() is None:
            run.kill()
            run.wait(timeout=5)
        if pid and running(pid):
            os.kill(pid, signal.SIGTERM)


def fake_process(proc, pid=7, ppid=1, start=99, state="S", comm="python", argv=(b"python",)):
    root = proc / str(pid)
    root.mkdir(parents=True, exist_ok=True)
    fields = [state, str(ppid), "7", "7", *["0"] * 15, str(start)]
    (root / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields))
    (root / "cmdline").write_bytes(b"\0".join(argv))
    return root


def test_process_identity_reads_start_time_and_parent_even_with_spaces_in_the_name(tmp_path):
    fake_process(tmp_path, comm="a (name)", argv=(b"python", b"-m", b"pytest"))
    assert server_lifetime.process(7, tmp_path) == {
        "pid": 7,
        "ppid": 1,
        "start": 99,
        "state": "S",
        "comm": "a (name)",
        "argv": [b"python", b"-m", b"pytest"],
    }


@pytest.mark.parametrize("stat", ["missing", "bad", "7 (python) S nope"])
def test_unreadable_process_is_gone(tmp_path, stat):
    root = tmp_path / "7"
    root.mkdir()
    if stat != "missing":
        (root / "stat").write_text(stat)
    assert server_lifetime.process(7, tmp_path) is None


@pytest.mark.parametrize("state,start,expected", [("S", 99, False), ("Z", 99, True), ("S", 100, True)])
def test_owner_identity_rejects_zombies_and_reused_pids(tmp_path, state, start, expected):
    fake_process(tmp_path, start=start, state=state)
    assert server_lifetime.ended((7, 99), tmp_path) is expected
    assert server_lifetime.ended((8, 99), tmp_path) is True


@pytest.mark.parametrize(
    "configured,expected",
    [
        ({"LEDGER_RUN_PID": "7"}, (7, 99)),
        ({"LEDGER_RUN_PID": "7", "LEDGER_RUN_START": "98"}, (7, 98)),
        ({"LEDGER_RUN_PID": "8"}, (8, 0)),
    ],
)
def test_explicit_owner_keeps_its_original_start_identity(tmp_path, configured, expected):
    fake_process(tmp_path)
    assert server_lifetime.owner(configured, tmp_path) == expected


@pytest.mark.parametrize(
    "comm,argv",
    [
        ("python", (b"python", b"-m", b"pytest")),
        ("python", (b"python", b"-m", b"mutmut")),
        ("bash", (b"bash",)),
        ("sh", (b"sh",)),
        ("zsh", (b"zsh",)),
        ("fish", (b"fish",)),
        ("codex", (b"codex",)),
        ("claude", (b"claude",)),
    ],
)
def test_owner_follows_client_processes_to_the_run(tmp_path, monkeypatch, comm, argv):
    fake_process(tmp_path, pid=9, ppid=7, start=101, argv=(b"python", b"ledger.py"))
    fake_process(tmp_path, comm=comm, argv=argv)
    monkeypatch.setattr(server_lifetime.os, "getppid", lambda: 9)
    assert server_lifetime.owner({}, tmp_path) == (7, 99)


@pytest.mark.parametrize("pid,expected", [(7, (7, 99)), (1, None), (8, None)])
def test_owner_without_a_known_runner_uses_the_live_parent(tmp_path, monkeypatch, pid, expected):
    fake_process(tmp_path)
    fake_process(tmp_path, pid=1)
    monkeypatch.setattr(server_lifetime.os, "getppid", lambda: pid)
    assert server_lifetime.owner({}, tmp_path) == expected


@pytest.mark.parametrize(
    "folder,port,expected",
    [
        ("development-ledger", 8765, True),
        ("development-ledger", 9999, False),
        ("scratch", 8765, False),
        ("scratch", 9999, False),
    ],
)
def test_only_the_shared_folder_and_port_are_exempt(tmp_path, monkeypatch, folder, port, expected):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert server_lifetime.shared(tmp_path / folder, port) is expected


def test_launch_environment_preserves_settings_and_pins_the_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(server_lifetime.os, "environ", {"SWARM_RELOAD": "0", "OTHER": "keep"})
    monkeypatch.setattr(server_lifetime, "owner", lambda env: (7, 99))
    assert server_lifetime.environment(tmp_path, 9999) == {
        "SWARM_RELOAD": "0",
        "OTHER": "keep",
        "LEDGER_RUN_PID": "7",
        "LEDGER_RUN_START": "99",
    }


def test_shared_launch_discards_test_owner_markers(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        server_lifetime.os, "environ", {"LEDGER_RUN_PID": "7", "LEDGER_RUN_START": "99", "OTHER": "keep"}
    )
    assert server_lifetime.environment(tmp_path / "development-ledger", 8765) == {"OTHER": "keep"}


def test_launch_without_an_owner_preserves_the_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(server_lifetime.os, "environ", {"OTHER": "keep"})
    monkeypatch.setattr(server_lifetime, "owner", lambda env: None)
    assert server_lifetime.environment(tmp_path, 9999) == {"OTHER": "keep"}


@pytest.mark.parametrize("ending", ["owner", "folder", "cwd", "stopped"])
def test_watch_stops_on_run_or_folder_end_and_cancels_on_server_exit(tmp_path, monkeypatch, ending):
    folder = tmp_path / "ledger"
    folder.mkdir()
    monkeypatch.setattr(
        server_lifetime, "environment", lambda folder, port: {"LEDGER_RUN_PID": "7", "LEDGER_RUN_START": "99"}
    )
    monkeypatch.setattr(server_lifetime, "owner", lambda env: (7, 99))
    monkeypatch.setattr(server_lifetime, "ended", lambda identity: ending == "owner")
    monkeypatch.setattr(server_lifetime.os, "environ", {})
    thread = Mock()
    monkeypatch.setattr(server_lifetime.threading, "Thread", thread)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: cwd))
    httpd = Mock()
    stopped = server_lifetime.watch(httpd, folder, 9999)
    assert json.loads((folder / ".server.owner.json").read_text()) == {"pid": 7, "start": 99}
    assert server_lifetime.os.environ == {"LEDGER_RUN_PID": "7", "LEDGER_RUN_START": "99"}
    thread.return_value.start.assert_called_once_with()
    assert thread.call_args.kwargs["daemon"] is True
    if ending == "folder":
        (folder / ".server.owner.json").unlink()
        folder.rmdir()
    elif ending == "cwd":
        cwd.rmdir()
    elif ending == "stopped":
        stopped.set()
    thread.call_args.kwargs["target"]()
    assert httpd.shutdown.call_count == (0 if ending == "stopped" else 1)


@pytest.mark.parametrize("kind", ["shared", "unowned"])
def test_watch_does_not_start_for_the_shared_service_or_an_unowned_foreground_service(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(server_lifetime, "shared", lambda folder, port: kind == "shared")
    monkeypatch.setattr(server_lifetime, "owner", lambda env: None)
    thread = Mock()
    monkeypatch.setattr(server_lifetime.threading, "Thread", thread)
    stopped = server_lifetime.watch(Mock(), tmp_path, 8765)
    assert not stopped.is_set()
    thread.assert_not_called()


def test_server_closes_its_socket_and_cancels_the_watch_on_exit(tmp_path, monkeypatch):
    from scripts.swarm_ledger import ledger_server

    monkeypatch.setattr(ledger_server.core, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(ledger_server, "PIDFILE", tmp_path / ".server.pid")
    monkeypatch.setattr(ledger_server.threading, "Thread", Mock())
    httpd = Mock()
    factory = Mock(return_value=httpd)
    monkeypatch.setattr(ledger_server, "ThreadingHTTPServer", factory)
    stopped = Mock()
    watch = Mock(return_value=stopped)
    monkeypatch.setattr(server_lifetime, "watch", watch)
    ledger_server.serve()
    factory.assert_called_once_with((ledger_server.HOST, ledger_server.PORT), ledger_server.Handler)
    watch.assert_called_once_with(httpd, tmp_path, ledger_server.PORT)
    assert (tmp_path / ".server.pid").read_text() == str(os.getpid())
    httpd.serve_forever.assert_called_once_with()
    stopped.set.assert_called_once_with()
    httpd.server_close.assert_called_once_with()


def test_hook_pins_the_run_before_detaching_the_ensure_process(tmp_path, monkeypatch):
    import ledger_link

    from scripts.swarm_ledger import ledger_hook

    (tmp_path / "sample.json").write_text("{}")
    monkeypatch.setattr(ledger_hook, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(ledger_link, "address", lambda: ("127.0.0.1", 9999))
    monkeypatch.setattr(ledger_hook.socket, "create_connection", Mock(side_effect=OSError))
    env = {"LEDGER_RUN_PID": "7", "LEDGER_RUN_START": "99"}
    environment = Mock(return_value=env)
    monkeypatch.setattr(server_lifetime, "environment", environment)
    start = Mock()
    monkeypatch.setattr(ledger_hook.subprocess, "Popen", start)
    ledger_hook.serve_ledgers()
    environment.assert_called_once_with(tmp_path, 9999)
    assert start.call_args.kwargs["env"] == env
    assert start.call_args.kwargs["start_new_session"] is True
    assert start.call_args.args[0][-1] == "--ensure"
