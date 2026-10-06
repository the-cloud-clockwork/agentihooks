import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.swarm_ledger import ledger_server as server


@pytest.mark.parametrize("release_socket", [False, True])
def test_client_waits_for_a_reloading_server_without_starting_another(tmp_path, monkeypatch, release_socket):
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        port = spare.getsockname()[1]
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
            while server.serving_dir() is None:
                assert time.monotonic() < deadline
                time.sleep(0.01)
            (tmp_path / "reload").touch()
            while not (tmp_path / "reloading").exists():
                assert time.monotonic() < deadline
                time.sleep(0.01)
            urlopen = server.urllib.request.urlopen

            def impatient(request, timeout):
                return urlopen(request, timeout=min(timeout, 0.05))

            with patch.object(server.urllib.request, "urlopen", side_effect=impatient):
                assert server.serving_dir() is None
                with patch.object(server.subprocess, "Popen") as start:
                    server.ensure()
                    start.assert_not_called()
            assert server.serving_dir() == str(tmp_path)
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
        before = time.monotonic()
        with patch.object(server.subprocess, "Popen") as start, pytest.raises(SystemExit, match="did not answer"):
            server.ensure()
        assert time.monotonic() - before < 0.5
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
        patch.object(server, "serving_dir", side_effect=[None, None, str(isolated_server)]),
        patch.object(server.subprocess, "Popen") as start,
    ):
        server.ensure()
    start.assert_called_once()
    assert start.call_args.args[0][-1] == "--serve"
    assert capsys.readouterr().out.strip() == server.BASE


def test_recovered_server_serving_another_folder_is_refused(isolated_server):
    with (
        patch.object(server, "serving_dir", side_effect=[None, "/another/ledger"]),
        patch.object(server, "port_held", return_value=True),
        patch.object(server.subprocess, "Popen") as start,
        pytest.raises(SystemExit, match="already serves /another/ledger"),
    ):
        server.ensure()
    start.assert_not_called()


def test_healthy_server_needs_no_start(isolated_server, capsys):
    with (
        patch.object(server, "serving_dir", return_value=str(isolated_server)),
        patch.object(server.subprocess, "Popen") as start,
    ):
        server.ensure()
    start.assert_not_called()
    assert capsys.readouterr().out.strip() == server.BASE


def test_bind_errors_other_than_occupied_are_reported(isolated_server):
    import errno

    with (
        patch.object(server, "serving_dir", return_value=None),
        patch.object(server.socket, "socket") as probe,
        patch.object(server.subprocess, "Popen") as start,
        pytest.raises(PermissionError),
    ):
        probe.return_value.__enter__.return_value.bind.side_effect = PermissionError(errno.EACCES, "denied")
        server.ensure()
    start.assert_not_called()
