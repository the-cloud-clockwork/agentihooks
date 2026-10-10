import http.client
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.swarm_ledger import ledger_link
from scripts.swarm_ledger import ledger_server as server
from tests import ledger_guard

ROOT = Path(__file__).resolve().parents[2]


class Stop(Exception):
    pass


def clean_environment(**values):
    base = {key: value for key, value in os.environ.items() if not key.startswith(("SWARM_", "LEDGER_"))}
    return {**base, "PYTHONPATH": str(ROOT), **values}


def test_the_bind_host_and_port_come_from_the_environment():
    env = {"LEDGER_HOST": "0.0.0.0", "LEDGER_PORT": "9100", "LEDGER_DIR": "/data"}
    assert ledger_link.address(env) == ("0.0.0.0", 9100)


def test_a_data_folder_may_serve_on_the_default_port():
    assert ledger_link.address({"LEDGER_DIR": "/data"}) == ("127.0.0.1", 8765)


@pytest.mark.parametrize("named", [False, True])
def test_the_shared_folder_keeps_its_fixed_port(named):
    folder = {"LEDGER_DIR": str(Path.home() / "development-ledger")} if named else {}
    env = {"LEDGER_PORT": "9100", **folder}
    assert ledger_link.address(env) == ("127.0.0.1", 8765)


def test_an_unset_public_url_adds_nothing():
    assert ledger_link.public_url({}) is None
    assert ledger_link.public_url({"SWARM_PUBLIC_URL": ""}) is None


@pytest.mark.parametrize("url", ["swarm.example.com", "ftp://swarm.example.com", "https://"])
def test_a_public_url_without_an_http_scheme_and_host_is_refused(url):
    with pytest.raises(ValueError) as refused:
        ledger_link.allowed_hosts({"SWARM_PUBLIC_URL": url})
    assert str(refused.value) == f"SWARM_PUBLIC_URL must be an http or https URL with a host, not {url!r}"


def test_a_plain_http_public_url_keeps_its_scheme():
    env = {"LEDGER_DIR": "/data", "SWARM_PUBLIC_URL": "http://swarm.local:8080"}
    assert ledger_link.allowed_origins(env) == {
        "http://127.0.0.1:8765",
        "http://localhost:8765",
        "http://swarm.local:8080",
    }


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
        "https://swarm.example.com",
        "http://swarm:8765",
        "https://swarm:8765",
        "http://ledger.lan",
        "https://ledger.lan",
    }


@pytest.mark.parametrize(("value", "expected"), [(None, False), ("0", False), ("true", False), ("1", True)])
def test_code_reload_runs_only_when_swarm_reload_is_one(value, expected):
    assert server.reloading({} if value is None else {"SWARM_RELOAD": value}) is expected


@pytest.mark.parametrize(("value", "calls"), [(None, []), ("0", []), ("1", [((42,),)])])
def test_the_seed_watcher_reloads_code_only_when_asked(monkeypatch, value, calls):
    if value is None:
        monkeypatch.delenv("SWARM_RELOAD", raising=False)
    else:
        monkeypatch.setenv("SWARM_RELOAD", value)
    monkeypatch.setattr(server, "code_stamp", lambda: 42)
    monkeypatch.setattr(server, "reload_if_changed", Mock())
    monkeypatch.setattr(server.ledger_bin, "tidy", Mock())
    monkeypatch.setattr(server, "bin_closed_without_swarm", Mock())
    monkeypatch.setattr(server, "sample_streams", Mock())
    monkeypatch.setattr(server.time, "sleep", Mock(side_effect=Stop))
    with pytest.raises(Stop):
        server.watch_ledgers()
    assert server.reload_if_changed.call_args_list == calls


@pytest.mark.parametrize(("value", "child"), [(None, "1"), ("0", "0")])
def test_the_workstation_server_reloads_code_unless_told_not_to(monkeypatch, tmp_path, value, child):
    if value is None:
        monkeypatch.delenv("SWARM_RELOAD", raising=False)
    else:
        monkeypatch.setenv("SWARM_RELOAD", value)
    monkeypatch.setattr(server.core, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(server, "LOGFILE", tmp_path / ".server.log")
    monkeypatch.setattr(server.ledger_link, "serving", Mock(side_effect=[None, str(tmp_path)]))
    monkeypatch.setattr(server, "port_held", lambda: False)
    monkeypatch.setattr(server, "server_process_alive", lambda: False)
    monkeypatch.setattr(server.subprocess, "Popen", Mock())
    server.ensure()
    assert server.subprocess.Popen.call_args.kwargs["env"]["SWARM_RELOAD"] == child


def test_a_folder_other_than_the_shared_one_may_serve_on_8765(monkeypatch, tmp_path):
    monkeypatch.setattr(server.core, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(server, "PORT", 8765)
    monkeypatch.setattr(server, "PIDFILE", tmp_path / ".server.pid")
    monkeypatch.setattr(server, "ThreadingHTTPServer", Mock())
    monkeypatch.setattr(server.threading, "Thread", Mock())
    server.serve()
    server.ThreadingHTTPServer.assert_called_once_with((server.HOST, 8765), server.Handler)


def test_files_under_the_agentihooks_home_never_configure_the_server(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("LEDGER_HOST=10.9.9.9\nSWARM_ALLOWED_HOSTS=file.example\n")
    (home / "swarm.env").write_text("LEDGER_HOST=10.9.9.8\nSWARM_PUBLIC_URL=https://file.example\n")
    env = clean_environment(
        AGENTIHOOKS_HOME=str(home),
        LEDGER_DIR=str(tmp_path / "ledgers"),
        LEDGER_PORT="9100",
        SWARM_ALLOWED_HOSTS="env.example",
    )
    probe = "import json; from scripts.swarm_ledger import ledger_server as s; print(json.dumps([s.HOST, sorted(s.ALLOWED_HOSTS)]))"
    out = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True, check=True).stdout
    assert json.loads(out.splitlines()[-1]) == ["127.0.0.1", ["127.0.0.1:9100", "env.example", "localhost:9100"]]


def page_request(host, origin=None):
    handler = Mock(headers={"Host": host, **({"Origin": origin} if origin else {})})
    handler.send.return_value = None
    return handler


@pytest.mark.parametrize("origin", [None, "null", "https://swarm.example.com"])
def test_the_page_check_passes_a_listed_host_and_origin(monkeypatch, origin):
    monkeypatch.setattr(server, "ALLOWED_HOSTS", {"swarm.example.com"})
    monkeypatch.setattr(server, "ALLOWED_ORIGINS", {"https://swarm.example.com"})
    handler = page_request("swarm.example.com", origin)
    assert server.Handler.refused(handler) is False
    handler.send.assert_not_called()


@pytest.mark.parametrize(
    ("host", "origin", "message"),
    [
        ("evil.example", None, "host not allowed"),
        ("swarm.example.com", "https://evil.example", "origin not allowed"),
    ],
)
def test_the_page_check_refuses_an_unlisted_host_or_origin(monkeypatch, host, origin, message):
    monkeypatch.setattr(server, "ALLOWED_HOSTS", {"swarm.example.com"})
    monkeypatch.setattr(server, "ALLOWED_ORIGINS", {"https://swarm.example.com"})
    handler = page_request(host, origin)
    assert server.Handler.refused(handler) is True
    handler.send.assert_called_once_with(403, message, "text/plain")


@contextmanager
def ledger_server(base, port):
    home = base / "home"
    home.mkdir(parents=True)
    env = clean_environment(
        AGENTIHOOKS_HOME=str(home),
        LEDGER_DIR=str(base / "ledgers"),
        LEDGER_HOST="127.0.0.1",
        LEDGER_PORT=str(port),
        SWARM_PUBLIC_URL="https://swarm.example.com",
        SWARM_ALLOWED_HOSTS="swarm.lan",
    )
    log_path = base / "server.log"
    with log_path.open("w") as log:
        child = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts" / "swarm_ledger" / "ledger_server.py"), "--serve"],
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 15
            own = (200, json.dumps({"dir": str(base / "ledgers")}).encode())
            while request(port, "/healthz", f"127.0.0.1:{port}") != own:
                assert child.poll() is None, log_path.read_text()
                assert time.monotonic() < deadline, log_path.read_text()
                time.sleep(0.05)
            yield port
        finally:
            child.terminate()
            child.wait(timeout=5)


@pytest.fixture
def hosted(tmp_path, ledger_port):
    with ledger_server(tmp_path, ledger_port) as port:
        yield port


def test_a_server_whose_port_another_server_holds_is_never_taken_for_its_own(tmp_path, ledger_port):
    with ledger_server(tmp_path / "other", ledger_port):
        with pytest.raises(AssertionError, match="Address already in use"):
            with ledger_server(tmp_path / "own", ledger_port):
                pass


def fetch(port, path, host, origin=None):
    headers = {"Host": host, **({"Origin": origin} if origin else {})}
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def request(port, path, host, origin=None):
    try:
        return fetch(port, path, host, origin)
    except OSError:
        return None, b""


def served(port, path, host, origin, log):
    try:
        return fetch(port, path, host, origin)
    except OSError as error:
        pytest.fail(f"{type(error).__name__}: {error}\nledger server log:\n{log.read_text()}")


def test_a_failed_request_names_its_error_and_the_server_log(tmp_path):
    log = tmp_path / "server.log"
    log.write_text("ledger server said this\n")
    with ledger_guard.reserve_port() as hold:
        with pytest.raises(pytest.fail.Exception, match=r"(?s)ConnectionRefusedError: .*ledger server said this"):
            served(hold.getsockname()[1], "/api/v1/ledgers", "swarm.lan", "http://swarm.lan", log)


@pytest.mark.parametrize(
    ("host", "origin"),
    [("swarm.example.com", "https://swarm.example.com"), ("swarm.lan", "http://swarm.lan")],
)
def test_a_listed_host_and_origin_are_served(hosted, tmp_path, host, origin):
    status, body = served(hosted, "/api/v1/ledgers", host, origin, tmp_path / "server.log")
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


@pytest.mark.parametrize(
    ("host", "origin", "reply"),
    [
        ("evil.example", None, (403, b"host not allowed")),
        ("swarm.example.com", "https://evil.example", (403, b"origin not allowed")),
        ("swarm.example.com", "http://swarm.example.com", (403, b"origin not allowed")),
    ],
)
def test_the_page_routes_refuse_an_unlisted_host_or_origin(hosted, host, origin, reply):
    assert request(hosted, "/healthz", host, origin) == reply


@pytest.mark.parametrize("origin", [None, "null", "https://swarm.example.com"])
def test_the_page_routes_serve_a_listed_host_and_origin(hosted, tmp_path, origin):
    status, body = request(hosted, "/healthz", "swarm.example.com", origin)
    assert (status, json.loads(body)) == (200, {"dir": str(tmp_path / "ledgers")})
