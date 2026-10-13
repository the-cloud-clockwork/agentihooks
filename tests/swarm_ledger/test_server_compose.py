import http.server
import inspect
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.swarm_ledger import ledger_server as server
from scripts.swarm_ledger import server_compose

ROOT = Path(__file__).resolve().parents[2]


def test_the_compose_file_is_the_one_beside_the_code():
    assert server_compose.COMPOSE_FILE == server_compose.primary(ROOT) / "deploy" / "compose" / "ledger.yaml"
    assert server_compose.ROOT == server_compose.primary(ROOT)
    assert server_compose.SERVICE == "ledger"


def test_a_worktree_resolves_to_its_primary_checkout(tmp_path):
    primary = tmp_path / "repo"
    worktree = tmp_path / "worktrees" / "repo" / "eng-1"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {primary}/.git/worktrees/eng-1\n")
    assert server_compose.primary(worktree) == primary.resolve()
    (primary / ".git").mkdir(parents=True)
    assert server_compose.primary(primary) == primary
    assert server_compose.primary(tmp_path / "installed") == tmp_path / "installed"
    (worktree / ".git").write_text("gitdir: ../../../repo/.git/worktrees/eng-1\n")
    assert server_compose.primary(worktree) == primary.resolve()


def shared(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    folder = tmp_path / "development-ledger"
    folder.mkdir()
    return folder


def test_only_the_shared_folder_with_docker_and_the_compose_file_uses_compose(tmp_path, monkeypatch):
    folder = shared(tmp_path, monkeypatch)
    docker = {"docker": "/usr/bin/docker"}.get
    present = tmp_path / "ledger.yaml"
    present.touch()
    assert server_compose.used(folder, {}, docker, present)
    assert not server_compose.used(tmp_path / "scratch", {}, docker, present)
    assert not server_compose.used(folder, {}, {}.get, present)
    assert not server_compose.used(folder, {}, docker, tmp_path / "gone.yaml")
    assert not server_compose.used(folder, {"LEDGER_SERVER_RUNTIME": "process"}, docker, present)
    assert server_compose.used(folder, {"LEDGER_SERVER_RUNTIME": "compose"}, docker, present)


def test_compose_runs_with_the_folder_the_code_and_this_user(tmp_path, monkeypatch):
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(server_compose.os, "getuid", lambda: 1234)
    monkeypatch.setattr(server_compose.os, "getgid", lambda: 5678)
    assert server_compose.start(tmp_path, {"HOME": "/h", "KEEP": "1"}, run) == ""
    assert server_compose.stop(tmp_path, {"HOME": "/h"}, run) == ""
    compose = ["docker", "compose", "-f", str(server_compose.COMPOSE_FILE)]
    assert [argv for argv, _ in calls] == [
        [*compose, "up", "-d", "ledger"],
        [*compose, "stop", "ledger"],
    ]
    first = calls[0][1]
    assert first["env"] == {
        "HOME": "/h",
        "KEEP": "1",
        "LEDGER_DIR": str(tmp_path),
        "LEDGER_CODE": str(server_compose.ROOT),
        "LEDGER_UID": "1234",
        "LEDGER_GID": "5678",
    }
    assert (first["capture_output"], first["text"], first["check"], first["timeout"]) == (True, True, False, 120)
    assert calls[1][1]["env"]["LEDGER_DIR"] == str(tmp_path)


def test_a_failed_or_hung_compose_names_its_error(tmp_path):
    def failed(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="x" * 300 + " no such image\n")

    def silent(argv, **kw):
        return subprocess.CompletedProcess(argv, 3, stdout="", stderr="")

    def hung(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 120)

    def missing(argv, **kw):
        raise FileNotFoundError("docker")

    assert server_compose.start(tmp_path, {}, failed) == ("x" * 300 + " no such image")[-200:]
    assert server_compose.start(tmp_path, {}, silent) == "docker compose exit code 3"
    assert "timed out" in server_compose.start(tmp_path, {}, hung)
    assert server_compose.stop(tmp_path, {}, missing) == "docker"


def test_compose_defaults_reach_the_real_environment_docker_and_runner():
    def defaults(function):
        return {name: p.default for name, p in inspect.signature(function).parameters.items() if name != "folder"}

    assert defaults(server_compose.used) == {
        "environ": os.environ,
        "which": shutil.which,
        "compose_file": server_compose.COMPOSE_FILE,
    }
    assert (
        defaults(server_compose.start)
        == defaults(server_compose.stop)
        == {
            "environ": os.environ,
            "run": subprocess.run,
        }
    )
    assert (server_compose.TIMEOUT_S, server_compose.ERROR_KEPT) == (120, 200)


@pytest.fixture
def compose_server(tmp_path, monkeypatch, ledger_port):
    monkeypatch.setattr(server.core, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(server, "PIDFILE", tmp_path / ".server.pid")
    monkeypatch.setattr(server, "LOGFILE", tmp_path / ".server.log")
    monkeypatch.setattr(server, "PORT", ledger_port)
    monkeypatch.setattr(server, "BASE", f"http://127.0.0.1:{ledger_port}")
    monkeypatch.setattr(server.server_compose, "used", lambda folder: folder == tmp_path)
    return tmp_path


def test_ensure_starts_the_shared_server_through_compose_and_never_spawns_it(compose_server, monkeypatch, capsys):
    answers = iter([None, None, str(compose_server)])
    monkeypatch.setattr(server.ledger_link, "serving", lambda **kw: next(answers))
    started, waited = [], []
    monkeypatch.setattr(server.server_compose, "start", lambda folder: started.append(folder) or "")
    monkeypatch.setattr(server, "wait_for", lambda folder: waited.append(folder) or 5.0)
    with patch.object(server.subprocess, "Popen") as spawn:
        server.ensure()
    spawn.assert_not_called()
    assert started == waited == [compose_server]
    assert capsys.readouterr().out == f"{server.BASE}\n"


def test_ensure_waits_past_the_compose_timeout_for_the_container(compose_server):
    assert server.COMPOSE_WAIT == 180.0 > server_compose.TIMEOUT_S
    assert server.wait_for(compose_server) == 180.0
    assert server.wait_for(compose_server / "scratch") == server.SERVER_WAIT


def test_a_compose_start_that_fails_ends_ensure_with_its_error(compose_server, monkeypatch):
    monkeypatch.setattr(server.ledger_link, "serving", lambda **kw: None)
    monkeypatch.setattr(server.server_compose, "start", lambda folder: "no such image")
    monkeypatch.setattr(server, "COMPOSE_WAIT", 0.5)
    with pytest.raises(SystemExit, match="could not start the ledger service: no such image"):
        server.ensure()


def test_a_bare_start_waits_while_the_port_or_the_old_server_is_still_there(tmp_path, monkeypatch):
    monkeypatch.setattr(server.core, "LEDGER_DIR", tmp_path)
    for held, alive in ((True, False), (False, True)):
        monkeypatch.setattr(server, "port_held", lambda held=held: held)
        monkeypatch.setattr(server, "server_process_alive", lambda alive=alive: alive)
        with patch.object(server.subprocess, "Popen") as spawn:
            assert server.start_server(False) is False
        spawn.assert_not_called()


def test_stop_stops_the_compose_service_and_clears_its_pidfile(compose_server, monkeypatch, capsys):
    (compose_server / ".server.pid").write_text("4194999")
    stopped = []
    monkeypatch.setattr(server.server_compose, "stop", lambda folder: stopped.append(folder) or "")
    monkeypatch.setattr(server.os, "kill", lambda *a: pytest.fail("a compose server is stopped through compose"))
    server.stop()
    assert stopped == [compose_server]
    assert capsys.readouterr().out == "stopped\n"
    assert not (compose_server / ".server.pid").exists()


def test_stop_also_ends_a_bare_server_left_from_before_the_cutover(compose_server, monkeypatch):
    monkeypatch.setattr(server.server_compose, "stop", lambda folder: "")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "ledger_server.py"])
    try:
        (compose_server / ".server.pid").write_text(str(child.pid))
        server.stop()
        assert child.wait(timeout=5) == -signal.SIGTERM
    finally:
        child.kill()
        child.wait(timeout=5)


def test_a_compose_stop_that_fails_names_its_error(compose_server, monkeypatch):
    monkeypatch.setattr(server.server_compose, "stop", lambda folder: "daemon down")
    with pytest.raises(SystemExit, match="could not stop the ledger service: daemon down"):
        server.stop()


def bare_handler(path, refused=False):
    handler = server.Handler.__new__(server.Handler)
    handler.path = path
    sent = []
    handler.send = lambda code, body, ctype: sent.append((code, body, ctype))
    handler.refused = lambda slug=None: refused
    return handler, sent


def test_a_probe_write_lands_under_the_ledger_write_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(server.core, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(server.core, "now_ms", lambda: 1234)
    written = []
    monkeypatch.setattr(
        server.core, "atomic_write", lambda path, text: written.append((path, text, server.core.LOCK.locked()))
    )
    handler, sent = bare_handler("/healthz?probe=1")
    handler.do_POST()
    assert written == [(tmp_path / ".healthz", "1234", True)]
    assert not server.core.LOCK.locked()
    assert sent == [(200, json.dumps({"dir": str(tmp_path)}), "application/json")]


def test_a_refused_probe_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(server.core, "LEDGER_DIR", tmp_path)
    handler, sent = bare_handler("/healthz", refused=True)
    handler.do_POST()
    assert sent == []
    assert not (tmp_path / ".healthz").exists()


def test_post_routes_pick_the_probe_media_and_artifact_writers():
    handler, _ = bare_handler("/")
    assert handler.post_route("/healthz") == handler.probe_write
    media = handler.post_route("/api/media/s/x")
    assert (media.func, media.args) == (handler.post_media, ("s/x",))
    artifact = handler.post_route("/api/artifacts/s")
    assert (artifact.func, artifact.args) == (handler.post_artifact, ("s",))
    assert handler.post_route("/api/bin") is None
    assert handler.post_route("/healthz/") is None


def test_the_server_exits_when_it_cannot_start_a_request_thread(monkeypatch):
    exits = []
    monkeypatch.setattr(server.os, "_exit", exits.append)
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler, bind_and_activate=False)

    def refused(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(server.threading.Thread, "start", refused)
    httpd.process_request(None, ("127.0.0.1", 1))
    httpd.server_close()
    assert exits == [server.THREADS_EXHAUSTED] == [75]
    assert issubclass(server.ThreadingHTTPServer, http.server.ThreadingHTTPServer)


def test_a_request_thread_that_starts_keeps_the_server_running(monkeypatch):
    monkeypatch.setattr(server.os, "_exit", lambda code: pytest.fail("the server exits only when no thread starts"))
    started = []
    monkeypatch.setattr(server.threading.Thread, "start", lambda self: started.append(self))
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler, bind_and_activate=False)
    request = object()
    httpd.process_request(request, ("127.0.0.1", 1))
    httpd.server_close()
    assert [thread._args for thread in started] == [(request, ("127.0.0.1", 1))]
