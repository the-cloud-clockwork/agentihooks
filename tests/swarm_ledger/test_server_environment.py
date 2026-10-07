import http.client
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.swarm_ledger import ledger_link
from scripts.swarm_ledger import ledger_server as server

ROOT = Path(__file__).resolve().parents[2]


class Stop(Exception):
    pass


def spare_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_the_bind_host_and_port_come_from_the_environment():
    env = {"LEDGER_HOST": "0.0.0.0", "LEDGER_PORT": "9100", "LEDGER_DIR": "/data"}
    assert ledger_link.address(env) == ("0.0.0.0", 9100)


def test_a_data_folder_may_serve_on_the_default_port():
    assert ledger_link.address({"LEDGER_DIR": "/data"}) == ("127.0.0.1", 8765)


def test_loopback_and_the_bind_host_are_always_allowed():
    env = {"LEDGER_HOST": "0.0.0.0", "LEDGER_PORT": "9100", "LEDGER_DIR": "/data"}
    assert ledger_link.allowed_hosts(env) == {"0.0.0.0:9100", "127.0.0.1:9100", "localhost:9100"}
    assert ledger_link.allowed_origins(env) == {
        "http://0.0.0.0:9100",
        "http://127.0.0.1:9100",
        "http://localhost:9100",
    }


def test_the_public_url_and_listed_hosts_join_the_allowed_sets():
    env = {
        "LEDGER_PORT": "9100",
        "LEDGER_DIR": "/data",
        "SWARM_PUBLIC_URL": "https://swarm.example.com/",
        "SWARM_ALLOWED_HOSTS": " swarm:8765 , ledger.lan ,",
    }
    assert ledger_link.allowed_hosts(env) == {
        "127.0.0.1:9100",
        "localhost:9100",
        "swarm.example.com",
        "swarm:8765",
        "ledger.lan",
    }
    assert ledger_link.allowed_origins(env) == {
        "http://127.0.0.1:9100",
        "http://localhost:9100",
        "http://swarm.example.com",
        "https://swarm.example.com",
        "http://swarm:8765",
        "https://swarm:8765",
        "http://ledger.lan",
        "https://ledger.lan",
    }


@pytest.mark.parametrize(("value", "expected"), [(None, False), ("0", False), ("true", False), ("1", True)])
def test_code_reload_runs_only_when_swarm_reload_is_one(value, expected):
    assert server.reloading({} if value is None else {"SWARM_RELOAD": value}) is expected


@pytest.mark.parametrize(("value", "calls"), [(None, 0), ("1", 1)])
def test_the_seed_watcher_reloads_code_only_when_asked(monkeypatch, value, calls):
    if value is None:
        monkeypatch.delenv("SWARM_RELOAD", raising=False)
    else:
        monkeypatch.setenv("SWARM_RELOAD", value)
    monkeypatch.setattr(server, "reload_if_changed", Mock())
    monkeypatch.setattr(server.ledger_bin, "tidy", Mock())
    monkeypatch.setattr(server.repository, "pages", lambda: [])
    monkeypatch.setattr(server, "sample_streams", Mock())
    monkeypatch.setattr(server.time, "sleep", Mock(side_effect=Stop))
    with pytest.raises(Stop):
        server.watch_seeds()
    assert server.reload_if_changed.call_count == calls


@pytest.mark.parametrize(("value", "child"), [(None, "1"), ("0", "0")])
def test_the_workstation_server_reloads_code_unless_told_not_to(monkeypatch, tmp_path, value, child):
    if value is None:
        monkeypatch.delenv("SWARM_RELOAD", raising=False)
    else:
        monkeypatch.setenv("SWARM_RELOAD", value)
    monkeypatch.setattr(server.core, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(server, "LOGFILE", tmp_path / ".server.log")
    monkeypatch.setattr(server, "serving_dir", Mock(side_effect=[None, str(tmp_path)]))
    monkeypatch.setattr(server, "port_held", lambda: False)
    monkeypatch.setattr(server, "server_process_alive", lambda: False)
    monkeypatch.setattr(server.subprocess, "Popen", Mock())
    server.ensure()
    assert server.subprocess.Popen.call_args.kwargs["env"]["SWARM_RELOAD"] == child


def test_a_folder_outside_the_home_may_serve_on_8765(monkeypatch, tmp_path):
    monkeypatch.setattr(server.core, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(server, "PORT", 8765)
    monkeypatch.setattr(server, "PIDFILE", tmp_path / ".server.pid")
    monkeypatch.setattr(server, "ThreadingHTTPServer", Mock())
    monkeypatch.setattr(server.threading, "Thread", Mock())
    server.serve()
    server.ThreadingHTTPServer.assert_called_once_with((server.HOST, 8765), server.Handler)


def test_the_environment_beats_a_file_under_the_agentihooks_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "swarm.env").write_text("LEDGER_HOST=10.9.9.9\nSWARM_ALLOWED_HOSTS=file.example\n")
    env = {
        **os.environ,
        "AGENTIHOOKS_HOME": str(home),
        "LEDGER_DIR": str(tmp_path / "ledgers"),
        "LEDGER_PORT": "9100",
        "LEDGER_HOST": "127.0.0.1",
        "SWARM_ALLOWED_HOSTS": "env.example",
        "PYTHONPATH": str(ROOT),
    }
    probe = "import json; from scripts.swarm_ledger import ledger_server as s; print(json.dumps([s.HOST, sorted(s.ALLOWED_HOSTS)]))"
    out = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True, check=True).stdout
    assert json.loads(out.splitlines()[-1]) == ["127.0.0.1", ["127.0.0.1:9100", "env.example", "localhost:9100"]]


@pytest.fixture
def hosted(tmp_path):
    port = spare_port()
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "AGENTIHOOKS_HOME": str(home),
        "LEDGER_DIR": str(tmp_path / "ledgers"),
        "LEDGER_HOST": "127.0.0.1",
        "LEDGER_PORT": str(port),
        "SWARM_PUBLIC_URL": "https://swarm.example.com",
        "SWARM_ALLOWED_HOSTS": "swarm.lan",
        "PYTHONPATH": str(ROOT),
    }
    env.pop("SWARM_RELOAD", None)
    log = (tmp_path / "server.log").open("w")
    child = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "swarm_ledger" / "ledger_server.py"), "--serve"],
        env=env,
        stdout=log,
        stderr=log,
    )
    deadline = time.monotonic() + 15
    while request(port, "/healthz", f"127.0.0.1:{port}")[0] != 200:
        assert child.poll() is None, (tmp_path / "server.log").read_text()
        assert time.monotonic() < deadline, (tmp_path / "server.log").read_text()
        time.sleep(0.05)
    yield port
    child.terminate()
    child.wait(timeout=5)
    log.close()


def request(port, path, host, origin=None):
    headers = {"Host": host, **({"Origin": origin} if origin else {})}
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        return response.status, response.read()
    except OSError:
        return None, b""
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("host", "origin"),
    [("swarm.example.com", "https://swarm.example.com"), ("swarm.lan", "http://swarm.lan")],
)
def test_a_listed_host_and_origin_are_served(hosted, host, origin):
    status, body = request(hosted, "/api/v1/ledgers", host, origin)
    assert status == 200
    assert json.loads(body)["data"] == []


@pytest.mark.parametrize(
    ("host", "origin", "message"),
    [
        ("evil.example", None, "Host not allowed"),
        ("swarm.example.com", "https://evil.example", "Origin not allowed"),
    ],
)
def test_an_unlisted_host_or_origin_is_refused(hosted, host, origin, message):
    status, body = request(hosted, "/api/v1/ledgers", host, origin)
    assert status == 403
    assert json.loads(body)["error"]["message"] == message


def test_an_unlisted_host_cannot_read_the_page_routes(hosted):
    assert request(hosted, "/healthz", "evil.example") == (403, b"host not allowed")
