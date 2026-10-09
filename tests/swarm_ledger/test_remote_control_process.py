import argparse
import itertools
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.swarm import cli, command_runner, commands
from scripts.swarm.store import RedisStore, SwarmConfig
from tests.inbox.test_wake import FakeHerdr
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.xdist_group("fakeredis")
ROOT = Path(__file__).resolve().parents[2]
BOOT = """
import json, shutil, sys, threading, time
from pathlib import Path
from http.server import ThreadingHTTPServer
sys.path.insert(0, str(Path(sys.argv[1]) / 'scripts' / 'swarm_ledger'))
import ledger_core, new_ledger
ledger_core.LEDGER_DIR = Path(sys.argv[2])
import ledger_server
for name in ('agentihooks', 'claude', 'gh'):
    assert shutil.which(name) is None
new_ledger.create('sw', {'title': 'Remote controls', 'overview': 'Control the owning hive', 'phases': [{'title': 'Controls', 'description': 'Remote tick'}]})
server = ThreadingHTTPServer(('127.0.0.1', 0), ledger_server.Handler)
ledger_server.ALLOWED_HOSTS.add(f'127.0.0.1:{server.server_address[1]}')
ledger_server.ALLOWED_ORIGINS.add(f'http://127.0.0.1:{server.server_address[1]}')
token = ledger_server.repository.token('sw')
Path(sys.argv[3]).write_text(json.dumps({'port': server.server_address[1], 'token': token, 'host': f'127.0.0.1:{ledger_server.PORT}'}))
def sample():
    while True:
        ledger_server.sample_streams()
        time.sleep(0.05)
threading.Thread(target=sample, daemon=True).start()
server.serve_forever()
"""


def wait_ready(ready, process, deadline):
    while process.poll() is None and time.monotonic() < deadline:
        try:
            return json.loads(ready.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.02)
    return None


def test_readiness_wait_returns_only_a_complete_reply(tmp_path):
    ready = tmp_path / "ready.json"
    writes = itertools.chain(["", '{"port": '], itertools.repeat('{"port": 1}'))

    def poll():
        ready.write_text(next(writes))

    assert wait_ready(ready, SimpleNamespace(poll=poll), time.monotonic() + 5) == {"port": 1}


@pytest.fixture
def remote_server(tmp_path, monkeypatch):
    import fakeredis
    import redis

    backend = fakeredis.TcpFakeServer(("127.0.0.1", 0), server_type="redis")
    thread = threading.Thread(target=backend.serve_forever, daemon=True)
    thread.start()
    url = f"redis://127.0.0.1:{backend.server_address[1]}/0"
    saved = RedisStore(redis.Redis.from_url(url, decode_responses=True, protocol=3))
    saved.create(SwarmConfig("sw", str(ROOT), 0, 0, state="running"))
    commands.bind(saved, "sw", "home")
    commands.publish(saved, "sw", {"tasks": {}, "agents": []}, {})
    monkeypatch.setenv("SWARM_HIVE_ID", "home")
    ready = tmp_path / "ready.json"
    env = {**os.environ, "PATH": "", "PYTHONPATH": str(ROOT), "AGENTIHOOKS_SWARM_REDIS_URL": url}
    log = (tmp_path / "server.log").open("w")
    process = subprocess.Popen(
        [sys.executable, "-c", BOOT, str(ROOT), str(tmp_path / "ledger"), str(ready)], env=env, stdout=log, stderr=log
    )
    try:
        found = wait_ready(ready, process, time.monotonic() + 10)
        assert found, (tmp_path / "server.log").read_text()

        def request(method, body=None):
            headers = {"Host": found["host"], "X-Ledger-Token": found["token"], "Content-Type": "application/json"}
            data = json.dumps(body).encode() if body else None
            req = urllib.request.Request(f"http://127.0.0.1:{found['port']}/api/swarm/sw", data, headers, method=method)
            with urllib.request.urlopen(req, timeout=5) as reply:
                result = json.load(reply)
                assert result is not None, (tmp_path / "server.log").read_text()
                return result

        yield saved, request, f"http://127.0.0.1:{found['port']}"
    finally:
        process.terminate()
        process.wait(timeout=5)
        log.close()
        backend.shutdown()
        backend.server_close()
        thread.join(timeout=5)


def test_tool_free_server_controls_the_swarm_through_its_owning_tick(remote_server, monkeypatch):
    saved, request, _ = remote_server
    pending = request("PUT", {"action": "pause"})
    assert pending["commands"][0]["state"] == "pending"
    assert saved.config("sw").state == "running"

    def dispatch(argv, env):
        assert argv == ["swarm", "sw", "pause"]
        assert env["AGENTIHOOKS_CONTROL_SOURCE"] == "page"
        cli.cmd_pause(saved, argparse.Namespace(slug="sw"))
        return ""

    monkeypatch.setattr(command_runner, "run", dispatch)
    actions = cli.run_tick(saved, "sw", FakeLedger([]), FakeRuntime(), FakeHerdr({}))
    assert "control swarm acknowledged" in actions
    acknowledged = request("GET")
    assert acknowledged["config"]["state"] == "paused"
    assert acknowledged["commands"][0]["id"] == pending["commands"][0]["id"]
    assert acknowledged["commands"][0]["state"] == "acknowledged"
    assert acknowledged["quota"] == commands.view(saved, "sw")["quota"]
