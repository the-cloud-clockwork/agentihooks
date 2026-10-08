import os
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import call, patch

import pytest

from scripts.swarm_ledger import ledger_server as server


@pytest.fixture(autouse=True)
def safe_process_signals(monkeypatch):
    kill = server.os.kill

    def send(pid, sig):
        assert sig in (0, signal.SIGTERM, signal.SIGKILL)
        return kill(pid, sig)

    monkeypatch.setattr(server.os, "kill", send)


@pytest.mark.parametrize("release_socket", [False, True])
def test_client_waits_for_a_reloading_server_without_starting_another(
    tmp_path, monkeypatch, release_socket, ledger_port
):
    port = ledger_port
    base = f"http://127.0.0.1:{port}"
    monkeypatch.setattr(server, "PORT", port)
    monkeypatch.setattr(server, "BASE", base)
    monkeypatch.setattr(server.core, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(server, "PIDFILE", tmp_path / ".server.pid")
    monkeypatch.setattr(server, "LOGFILE", tmp_path / ".server.log")
    script = tmp_path / "ledger_server.py"
    script.write_text(
        "import os, sys, threading, time\n"
        "from pathlib import Path\n"
        "from scripts.swarm_ledger import ledger_server as server\n"
        "httpd = server.ThreadingHTTPServer((server.HOST, server.PORT), server.Handler)\n"
        "server.PIDFILE.write_text(str(os.getpid()))\n"
        "threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True).start()\n"
        "while not (server.core.LEDGER_DIR / 'reload').exists(): time.sleep(0.01)\n"
        "httpd.shutdown()\n"
        "if os.environ['RELEASE_SOCKET'] == 'True': httpd.server_close()\n"
        "(server.core.LEDGER_DIR / 'reloading').touch()\n"
        "time.sleep(0.3)\n"
        "server.reload_if_changed(0, (server.core.LEDGER_DIR,))\n"
    )
    env = {**os.environ, "LEDGER_DIR": str(tmp_path), "LEDGER_PORT": str(port)}
    env["RELEASE_SOCKET"] = str(release_socket)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    with (tmp_path / ".server.log").open("w") as log:
        child = subprocess.Popen([sys.executable, str(script)], env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 5
            while server.ledger_link.serving(url=base) is None:
                assert time.monotonic() < deadline
                time.sleep(0.01)
            (tmp_path / "reload").touch()
            while not (tmp_path / "reloading").exists():
                assert time.monotonic() < deadline
                time.sleep(0.01)
            urlopen = server.ledger_link.urllib.request.urlopen

            def impatient(request, timeout):
                return urlopen(request, timeout=min(timeout, 0.05))

            with patch.object(server.ledger_link.urllib.request, "urlopen", side_effect=impatient):
                assert server.ledger_link.serving(url=base) is None
                with patch.object(server.subprocess, "Popen") as start:
                    server.ensure()
                    start.assert_not_called()
            assert server.ledger_link.serving(url=base) == str(tmp_path)
            assert child.poll() is None
        finally:
            child.terminate()
            child.wait(timeout=5)
    assert "Address already in use" not in (tmp_path / ".server.log").read_text()


@pytest.fixture
def isolated_server(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "HOST", "127.0.0.1")
    monkeypatch.setattr(server.core, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(server, "PIDFILE", tmp_path / ".server.pid")
    monkeypatch.setattr(server, "LOGFILE", tmp_path / ".server.log")
    monkeypatch.setattr(server, "SERVER_WAIT", 0.5)
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        monkeypatch.setattr(server, "PORT", spare.getsockname()[1])
    monkeypatch.setattr(server, "BASE", f"http://127.0.0.1:{server.PORT}")
    return tmp_path


def test_occupied_unresponsive_port_times_out_without_starting(isolated_server, monkeypatch):
    monkeypatch.setattr(server, "SERVER_WAIT", 0.15)
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        monkeypatch.setattr(server, "PORT", held.getsockname()[1])
        monkeypatch.setattr(server, "BASE", f"http://127.0.0.1:{server.PORT}")
        clock = time.monotonic
        before = clock()

        def bounded_clock():
            now = clock()
            assert now - before < 0.5
            return now

        with (
            patch.object(server.time, "monotonic", side_effect=bounded_clock),
            patch.object(server.subprocess, "Popen") as start,
            pytest.raises(SystemExit, match="did not answer"),
        ):
            server.ensure()
        assert time.monotonic() - before < 0.5
        start.assert_not_called()


def test_a_listening_silent_port_waits_out_the_deadline_without_starting(isolated_server, monkeypatch):
    monkeypatch.setattr(server, "SERVER_WAIT", 0.15)
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen()
        monkeypatch.setattr(server, "PORT", silent.getsockname()[1])
        monkeypatch.setattr(server, "BASE", f"http://127.0.0.1:{server.PORT}")
        with (
            patch.object(server.time, "monotonic", side_effect=[0, 0.05, 1]),
            patch.object(server.time, "sleep"),
            patch.object(server, "port_held", wraps=server.port_held) as held,
            patch.object(server.subprocess, "Popen") as start,
            pytest.raises(SystemExit) as refused,
        ):
            server.ensure()
    assert str(refused.value) == f"ledger server did not answer on {server.BASE}; see {server.LOGFILE}"
    held.assert_called_once_with()
    start.assert_not_called()


@pytest.mark.parametrize(
    "pid", [None, "invalid", "999999999", str(os.getpid())], ids=["missing", "invalid", "stale", "unrelated"]
)
def test_free_port_starts_once_and_waits_for_readiness(isolated_server, monkeypatch, pid, capsys):
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        port = spare.getsockname()[1]
    monkeypatch.setattr(server, "PORT", port)
    if pid is not None:
        server.PIDFILE.write_text(pid)
    with (
        patch.object(server.ledger_link, "serving", side_effect=[None, None, str(isolated_server)]),
        patch.object(server.subprocess, "Popen") as start,
    ):
        server.ensure()
    start.assert_called_once()
    assert start.call_args.args[0][-1] == "--serve"
    options = start.call_args.kwargs
    assert options["stdout"].name == str(server.LOGFILE)
    assert options["stderr"] is options["stdout"]
    assert options["stdin"] == subprocess.DEVNULL
    assert options["start_new_session"] is True
    assert capsys.readouterr().out.strip() == server.BASE


def test_recovered_server_serving_another_folder_is_refused(isolated_server):
    with (
        patch.object(server.ledger_link, "serving", side_effect=[None, "/another/ledger"]),
        patch.object(server, "port_held", return_value=True),
        patch.object(server.subprocess, "Popen") as start,
        pytest.raises(SystemExit, match="already serves /another/ledger"),
    ):
        server.ensure()
    start.assert_not_called()


def test_healthy_server_needs_no_start(isolated_server, capsys):
    with (
        patch.object(server.ledger_link, "serving", return_value=str(isolated_server)),
        patch.object(server.subprocess, "Popen") as start,
    ):
        server.ensure()
    start.assert_not_called()
    assert capsys.readouterr().out.strip() == server.BASE


def test_bind_errors_other_than_occupied_are_reported(isolated_server):
    import errno

    with (
        patch.object(server.ledger_link, "serving", return_value=None),
        patch.object(server.socket, "socket") as probe,
        patch.object(server.subprocess, "Popen") as start,
        pytest.raises(PermissionError),
    ):
        probe.return_value.__enter__.return_value.bind.side_effect = PermissionError(errno.EACCES, "denied")
        server.ensure()
    start.assert_not_called()


def test_start_creates_missing_parent_folders(isolated_server, monkeypatch):
    folder = isolated_server / "parent" / "ledger"
    monkeypatch.setattr(server.core, "LEDGER_DIR", folder)
    monkeypatch.setattr(server, "PIDFILE", folder / ".server.pid")
    monkeypatch.setattr(server, "LOGFILE", folder / ".server.log")
    with (
        patch.object(server.ledger_link, "serving", side_effect=[None, str(folder)]),
        patch.object(server.subprocess, "Popen") as start,
    ):
        server.ensure()
    assert folder.is_dir()
    start.assert_called_once()


def test_deadline_is_enforced_at_exact_expiry(isolated_server):
    with (
        patch.object(server.ledger_link, "serving", return_value=None),
        patch.object(server.time, "monotonic", side_effect=[100, 100.5]),
        patch.object(server.subprocess, "Popen") as start,
        pytest.raises(SystemExit, match="did not answer"),
    ):
        server.ensure()
    start.assert_not_called()


def test_health_requests_use_short_timeouts(isolated_server):
    with patch.object(server.ledger_link.urllib.request, "urlopen") as health:
        health.return_value.__enter__.return_value.read.return_value = f'{{"dir": "{isolated_server}"}}'.encode()
        server.ensure()
        health.assert_called_once_with(f"{server.BASE}/healthz", timeout=1)
    with (
        patch.object(server.time, "monotonic", return_value=100),
        patch.object(server, "SERVER_WAIT", 5),
        patch.object(server.ledger_link, "serving", side_effect=[None, str(isolated_server)]) as ready,
        patch.object(server.subprocess, "Popen"),
    ):
        server.ensure()
    assert ready.call_args_list == [call(url=server.BASE), call(timeout=1, url=server.BASE)]


def test_a_port_answering_an_error_is_refused_without_waiting(isolated_server, monkeypatch):
    class Refusing(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(404)
            self.end_headers()

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Refusing)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setattr(server, "BASE", f"http://127.0.0.1:{httpd.server_address[1]}")
    try:
        with (
            patch.object(server.time, "sleep") as wait,
            patch.object(server.subprocess, "Popen") as start,
            pytest.raises(SystemExit) as refused,
        ):
            server.ensure()
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert str(refused.value) == (
        f"{server.BASE} already serves no ledger folder, not {isolated_server}; stop that ledger server first"
    )
    wait.assert_not_called()
    start.assert_not_called()


@pytest.mark.parametrize("unreadable", [False, True])
def test_live_reexec_with_an_empty_command_line_waits(isolated_server, unreadable):
    server.PIDFILE.write_text(str(os.getpid()))
    with (
        patch.object(server, "port_held", return_value=False),
        patch.object(Path, "read_bytes", return_value=b"", side_effect=OSError() if unreadable else None),
        patch.object(server.ledger_link, "serving", side_effect=[None, str(isolated_server)]),
        patch.object(server.subprocess, "Popen") as start,
    ):
        server.ensure()
    start.assert_not_called()


@pytest.mark.parametrize("platform", ["darwin", "freebsd"])
def test_reload_without_linux_process_files_does_not_start_another(isolated_server, platform):
    server.PIDFILE.write_text(str(os.getpid()))
    with (
        patch.object(server.sys, "platform", platform),
        patch.object(server, "port_held", return_value=False),
        patch.object(Path, "read_bytes", side_effect=FileNotFoundError()) as process_files,
        patch.object(server.ledger_link, "serving", side_effect=[None, str(isolated_server)]),
        patch.object(server.subprocess, "Popen") as start,
    ):
        server.ensure()
    start.assert_not_called()
    process_files.assert_not_called()


@pytest.mark.parametrize(
    "cmdline, alive", [(b"python ledger_server.py --serve", True), (b"python unrelated.py", False)]
)
def test_linux_server_identity_is_preserved(isolated_server, cmdline, alive):
    server.PIDFILE.write_text(str(os.getpid()))
    with (
        patch.object(server.sys, "platform", "linux"),
        patch.object(Path, "read_bytes", return_value=cmdline) as process_files,
    ):
        assert server.server_process_alive() is alive
    process_files.assert_called_once_with()


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_dead_server_pid_is_not_alive(isolated_server, platform):
    server.PIDFILE.write_text("999999")
    with (
        patch.object(server.sys, "platform", platform),
        patch.object(server.os, "kill", side_effect=ProcessLookupError()) as probe,
        patch.object(Path, "read_bytes") as process_files,
    ):
        assert server.server_process_alive() is False
    probe.assert_called_once_with(999999, 0)
    process_files.assert_not_called()
