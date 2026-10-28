import ast
import json
import time
from pathlib import Path

import pytest

from scripts.events import memory
from scripts.events.bus import StateConflict
from scripts.events.config import BusConfig
from scripts.hive import auth, hive_state, registry
from scripts.swarm.keyspace import ROOT

pytestmark = pytest.mark.unit

PUBLIC = "redis://hive.example:6380/2"
SOURCES = Path(__file__).resolve().parents[2] / "scripts" / "hive"


@pytest.fixture
def client():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


@pytest.fixture
def bus(monkeypatch):
    opened = memory.open_bus(BusConfig())
    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "bus")
    monkeypatch.setattr(hive_state, "_OPENED", {})
    monkeypatch.setattr(hive_state.state, "open_state", opened.state)
    yield opened
    opened.close()


@pytest.fixture
def on_redis(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "redis")


def _imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("module", ["registry.py", "auth.py", "hive_state.py"])
def test_the_hive_registry_and_auth_modules_import_no_redis_client(module):
    assert not {name for name in _imports(SOURCES / module) if name == "redis" or name.startswith("redis.")}


def test_a_hive_record_round_trips_through_the_bus_state_store_and_never_reaches_redis(bus, client):
    record = registry.update(client, "box", ["max-agents=4", "capabilities=gpu"])

    assert record["max_agents"] == 4
    assert registry.show(client, "box") == record
    assert [hive["id"] for hive in registry.hives(client)] == ["box"]
    assert client.keys("*") == []
    assert bus.state(hive_state.RECORDS).get("box") is not None


def test_a_bus_heartbeat_refreshes_only_a_seeded_record(bus, client):
    registry.touch(client, "ghost", 5)
    assert registry.show(client, "ghost") is None

    registry.update(client, "box", ["name=box"])
    registry.touch(client, "box", 7)
    assert registry.show(client, "box")["heartbeat_at"] == 7


def test_a_bus_beat_seeds_defaults_and_keeps_operator_settings(bus, client):
    registry.update(client, "box", ["max-agents=3"])
    registry.beat(client, "box", {"slots": [{"harness": "claude"}], "heartbeat_at": 9})

    record = registry.show(client, "box")
    assert (record["max_agents"], record["slots"], record["heartbeat_at"]) == (3, [{"harness": "claude"}], 9)


def test_a_concurrent_write_is_retried_so_neither_change_is_lost(bus, client, monkeypatch):
    store = bus.state(hive_state.RECORDS)
    registry.update(client, "box", ["name=first"])
    real = store.update
    raced = []

    def racing(key, value, revision):
        if not raced:
            raced.append(True)
            entry = store.get(key)
            real(key, entry.value.replace(b'"first"', b'"other"'), entry.revision)
        return real(key, value, revision)

    monkeypatch.setattr(store, "update", racing)
    registry.update(client, "box", ["max-agents=5"])

    record = registry.show(client, "box")
    assert (record["name"], record["max_agents"]) == ("other", 5)


def test_a_forgotten_hive_leaves_the_record_and_the_index(bus, client):
    registry.update(client, "box", ["name=box"])
    registry.forget(client, "box")

    assert registry.show(client, "box") is None
    assert registry.hives(client) == []


def test_a_redis_record_lives_in_the_hive_own_state_store_and_a_legacy_hash_still_reads(on_redis, client):
    client.hset(f"{ROOT}:hive:old", mapping={"name": "old", "max_agents": "2", "heartbeat_at": "1"})
    client.sadd(auth.INDEX, "old")

    assert registry.show(client, "old")["max_agents"] == 2
    registry.update(client, "old", ["max-agents=6"])
    assert client.hget(f"{ROOT}:swarm:state:hive-old:current", "old")
    assert registry.show(client, "old")["max_agents"] == 6
    assert [hive["id"] for hive in registry.hives(client)] == ["old"]


def test_a_member_acl_user_may_write_its_own_state_store(client):
    assert f"~{ROOT}:swarm:state:hive-abc:*" in auth._acl_keys("abc")


def test_bus_credentials_resolve_through_the_state_store_and_keep_no_redis_keys(bus, client):
    grant = auth.mint(client, "laptop", PUBLIC)

    assert auth.member_of(client, grant["ledger_credential"]) == grant["id"]
    assert auth.ledger_member(client, grant["ledger_credential"]) == grant["id"]
    assert auth.member(client, grant["id"])["name"] == "laptop"
    token = auth.issue_agent(client, grant["id"], "alpha", "engineer@1")
    assert auth.agent_member(client, token, "alpha", "engineer@1") == grant["id"]
    assert auth.agent_member(client, token, "alpha", "engineer@2") is None
    assert client.keys("*") == []

    auth.revoke(client, grant["id"])
    assert auth.ledger_member(client, grant["ledger_credential"]) is None
    assert auth.member(client, grant["id"]) is None
    with pytest.raises(auth.HiveError):
        auth.issue_agent(client, grant["id"], "alpha", "engineer@1")


def test_bus_controller_and_service_credentials_expire_by_their_recorded_time(bus, client, monkeypatch):
    controller = auth.issue_controller(client)
    service = auth.issue_service(client, "hub-one")

    assert auth.controller(client, controller)
    assert auth.controller(client, service)
    assert not auth.controller(client, controller + "x")
    later = time.time() + auth.SERVICE_TTL_S + 1
    monkeypatch.setattr(auth.time, "time", lambda: later)
    assert auth.controller(client, controller)
    assert not auth.controller(client, service)


def test_redis_credentials_stay_outside_the_member_readable_keyspace(on_redis, client):
    grant = auth.mint(client, "laptop", PUBLIC)

    from scripts.events.redis_state import RedisStateStore

    digest = auth._digest(grant["ledger_credential"])
    assert RedisStateStore(client, "credentials", auth.PREFIX).get(f"ledger.{digest}") is not None
    index = f"{ROOT}:swarm:state:hive-{hive_state.INDEX}:"
    outside = {key for key in client.keys("*") if not key.startswith((f"{auth.PREFIX}:", index))}
    assert outside == {auth.INDEX}
    assert client.smembers(auth.INDEX) == {grant["id"]}


def test_a_legacy_redis_member_still_authenticates(on_redis, client):
    ledger = "legacy-ledger"
    token = "hive.m1.secret"
    client.hset(f"{auth.PREFIX}:member:m1", mapping={"name": "old", "ledger": auth._digest(ledger)})
    client.hset(f"{auth.PREFIX}:member:m1", f"agent:{auth._digest(token)}", '["alpha", "engineer@1"]')
    client.set(f"{auth.PREFIX}:ledger:{auth._digest(ledger)}", "m1")
    client.set(f"{auth.PREFIX}:controller", auth._digest("boss"))

    assert auth.ledger_member(client, ledger) == "m1"
    assert auth.agent_member(client, token, "alpha", "engineer@1") == "m1"
    assert auth.controller(client, "boss")
    assert auth.member(client, "m1")["name"] == "old"


def test_amend_gives_up_after_its_tries(bus):
    store = bus.state("conflicts")

    class Always:
        def get(self, key):
            return store.get(key)

        def create(self, key, value):
            raise StateConflict("always")

    with pytest.raises(StateConflict):
        hive_state.amend(Always(), "k", lambda current: {"v": 1})


def test_bus_registry_heartbeats_gate_launch_slots(client, bus):
    import time

    from scripts.routing.reservation import LaunchSlots

    now = int(time.time() * 1000)
    registry.beat(client, "worker", {"heartbeat_at": now})
    hive_state.indexed(client, "worker", True)
    client.set(f"{registry.key('worker')}:sessions", json.dumps({"claude:one": 1}))
    assert not client.smembers(registry.INDEX)
    slots = LaunchSlots(client)
    assert not slots.reserve("claude:one", "first", 1, 1000)
    registry.beat(client, "worker", {"heartbeat_at": now - registry.LIVE_MS - 1})
    assert slots.reserve("claude:one", "first", 1, 1000)


def test_bus_registry_index_keeps_reports_visible(client, bus):
    from types import SimpleNamespace

    from scripts.hive.reports import Reports

    hive_state.indexed(client, "worker", True)
    reports = Reports(client)
    monkey_report = {"names": ["engineer"]}
    from scripts.hive.reports import key

    client.set(key("worker"), json.dumps(monkey_report))
    assert reports.live() == {"worker": monkey_report}
    client.delete(key("worker"))
    agent = SimpleNamespace(state="finished", hive="worker", execution_id="execution", generation=1, runtime_target={})
    assert not reports.removed(agent, 1000)
    assert not reports.removed(agent, 1000 + registry.LIVE_MS)
