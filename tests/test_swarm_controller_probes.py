import json
import os

import pytest

from scripts.swarm import controller_probes
from scripts.swarm.controller import main

pytestmark = pytest.mark.unit
SLUG = "fixture"
ENV = {"AGENTIHOOKS_CONTROL_API_PORT": "8780", "AGENTIHOOKS_CONTROL_SWARM": SLUG}


@pytest.fixture
def server():
    import fakeredis

    return fakeredis.FakeServer()


@pytest.fixture
def store(server):
    import fakeredis

    from scripts.swarm.store import RedisStore, SwarmConfig

    found = RedisStore(fakeredis.FakeRedis(server=server, decode_responses=True))
    found.create(SwarmConfig(SLUG, "agentihooks", 2, 0))
    return found


class Fetch:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    def __call__(self, url, timeout):
        self.calls.append((url, timeout))
        if self.error is not None:
            raise self.error


def held(store, owner="hive-fixture"):
    from scripts.swarm import lease

    assert lease.acquire(store, SLUG, owner) is not None


def test_live_passes_when_the_open_endpoint_answers_and_every_lease_is_held(store, capsys):
    held(store)
    fetch = Fetch()

    assert controller_probes.live(ENV, store, fetch, "hive-fixture") == 0
    assert fetch.calls == [("http://127.0.0.1:8780/v2/health/live", 3)]
    assert capsys.readouterr().out == "controller: live\n"


def test_live_fails_when_the_lease_holder_does_not_answer(store, capsys):
    held(store)

    assert controller_probes.live(ENV, store, Fetch(OSError("connection refused")), "hive-fixture") == 1
    assert capsys.readouterr().out == "controller: the health endpoint did not answer: OSError: connection refused\n"


def test_live_passes_a_standby_while_another_hive_holds_the_lease(store, capsys):
    held(store, "hive-rival")

    assert controller_probes.live(ENV, store, Fetch(OSError("connection refused")), "hive-fixture") == 0
    assert capsys.readouterr().out == "controller: standby while hive-rival holds the fixture lease\n"


def test_live_takes_its_own_hive_from_the_controller_environment(store, monkeypatch, capsys):
    monkeypatch.setenv("SWARM_HIVE_ID", "hive-fixture")
    held(store)

    assert controller_probes.live(ENV, store, Fetch(OSError("connection refused"))) == 1
    assert capsys.readouterr().out == "controller: the health endpoint did not answer: OSError: connection refused\n"


def test_live_fails_when_the_served_swarm_has_no_lease_at_all(store, capsys):
    held(store)
    environ = {**ENV, "AGENTIHOOKS_CONTROL_SWARM": "absent"}

    assert controller_probes.live(environ, store, Fetch(OSError("connection refused")), "hive-fixture") == 1
    assert capsys.readouterr().out == "controller: the health endpoint did not answer: OSError: connection refused\n"


def test_live_fails_when_a_swarm_has_no_live_controller_lease(store, capsys):
    fetch = Fetch()

    assert controller_probes.live(ENV, store, fetch, "hive-fixture") == 1
    assert fetch.calls == []
    assert capsys.readouterr().out == "controller: no live controller lease for ['fixture']\n"


def test_live_leaves_a_host_ticked_swarm_without_a_lease_to_its_host(store, capsys):
    from scripts.swarm.store import SwarmConfig

    held(store)
    store.create(SwarmConfig("workstation", "agentihooks", 2, 0))
    environ = {**ENV, "AGENTIHOOKS_CONTROL_HOST_SWARMS": "other,workstation"}

    assert controller_probes.live(environ, store, Fetch(), "hive-fixture") == 0
    assert capsys.readouterr().out == "controller: live\n"


def test_live_still_fails_on_a_swarm_left_off_the_host_ticked_list(store, capsys):
    from scripts.swarm.store import SwarmConfig

    held(store)
    store.create(SwarmConfig("workstation", "agentihooks", 2, 0))
    environ = {**ENV, "AGENTIHOOKS_CONTROL_HOST_SWARMS": "other"}

    assert controller_probes.live(environ, store, Fetch(), "hive-fixture") == 1
    assert capsys.readouterr().out == "controller: no live controller lease for ['workstation']\n"


def test_live_requires_every_lease_while_no_host_ticked_list_is_set(store, monkeypatch, capsys):
    from scripts.swarm.store import SwarmConfig

    monkeypatch.delenv("AGENTIHOOKS_CONTROL_HOST_SWARMS", raising=False)
    held(store)
    store.create(SwarmConfig("XXXX", "agentihooks", 2, 0))

    assert controller_probes.live(ENV, store, Fetch(), "hive-fixture") == 1
    assert capsys.readouterr().out == "controller: no live controller lease for ['XXXX']\n"


@pytest.mark.parametrize(
    ("host_swarms", "expected"),
    [("", ["a", "b", "c"]), ("b", ["a", "c"]), ("c,a", ["b"]), ("a,b,c", []), (" a , c", ["b"])],
)
def test_ticked_drops_exactly_the_host_ticked_swarms_in_order(host_swarms, expected):
    assert controller_probes.ticked(["a", "b", "c"], host_swarms) == expected


def test_ready_passes_on_the_deep_report_while_the_api_and_redis_answer(store, capsys):
    fetch = Fetch()

    assert controller_probes.ready(ENV, store, fetch) == 0
    assert fetch.calls == [("http://127.0.0.1:8780/v2/health/live", 3)]
    assert json.loads(capsys.readouterr().out) == {"probe": "deep", "status": "ok", "redis": "ok", "remediation": []}


def test_ready_fails_while_the_control_api_does_not_answer(store, capsys):
    assert controller_probes.ready(ENV, store, Fetch(OSError("connection refused"))) == 1
    assert capsys.readouterr().out == "controller: the control API does not answer: OSError: connection refused\n"


def test_ready_fails_with_its_hint_when_redis_is_unavailable(store, server, capsys):
    server.connected = False

    assert controller_probes.ready(ENV, store, Fetch()) == 1
    report = json.loads(capsys.readouterr().out)
    assert (report["status"], report["redis"]) == ("degraded", "unavailable")
    assert [hint["class"] for hint in report["remediation"]] == ["dependency_unavailable"]
    assert all(hint["hint"] for hint in report["remediation"])


@pytest.mark.parametrize("action", ["live", "ready"])
def test_the_controller_command_runs_each_probe(monkeypatch, action):
    calls = []
    monkeypatch.setattr("scripts.swarm.controller.connect", lambda: "store")
    monkeypatch.setattr(controller_probes, action, lambda environ, store: calls.append((environ, store)) or 3)

    assert main([action]) == 3
    assert calls == [(os.environ, "store")]


@pytest.mark.parametrize("action", ["live", "ready"])
def test_the_controller_command_fails_its_probe_when_redis_refuses_the_connection(monkeypatch, capsys, action):
    from scripts.swarm.store import SwarmError

    def refused():
        raise SwarmError("Redis is unreachable")

    monkeypatch.setattr("scripts.swarm.controller.connect", refused)

    assert main([action]) == 1
    assert capsys.readouterr().out == "controller: Redis is unreachable\n"
