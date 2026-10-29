import json

import pytest

from scripts.events import memory
from scripts.events.config import BusConfig
from scripts.events.redis_state import RedisStateStore
from scripts.swarm import naming
from scripts.swarm.store import RedisStore, SwarmConfig

pytestmark = pytest.mark.unit


@pytest.fixture
def client():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


@pytest.fixture
def bus(monkeypatch):
    opened = memory.open_bus(BusConfig())
    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "bus")
    monkeypatch.setattr(naming, "_OPENED", {})
    monkeypatch.setattr(naming.state, "open_state", opened.state)
    yield opened
    opened.close()


@pytest.fixture
def on_redis(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "redis")


def _legacy(*parts):
    return naming.NameRegistry.key(*parts)


def test_the_registry_runs_on_the_bus_with_no_redis_client(bus):
    names = naming.NameRegistry(None, mint=iter(["abcdef"]).__next__)
    assert names.mint_code("sw", "sw", "/repo") == "abcdef"
    first, second = names.next("sw", "eng", at=5), names.next("sw", "eng", at=6)
    ci = names.next("sw", "ci", at=7)
    assert (first, second, ci) == ("engineer@abcdef-0001", "engineer@abcdef-0002", "ci@abcdef-0001")
    names.note(first, session_id="s1")
    names.retire(first, 9)
    names.alias("sw-eng-1", second)
    assert names.resolve("sw-eng-1") == second
    assert names.resolve_many(["sw-eng-1", "x"]) == {"sw-eng-1": second, "x": "x"}
    assert names.aliases(second) == ["sw-eng-1"]
    assert names.entry(first) == {
        "name": first,
        "type": "engineer",
        "number": 1,
        "code": "abcdef",
        "swarm": "sw",
        "session_id": "s1",
        "spawned_at": 5,
        "retired_at": 9,
    }
    assert [row["name"] for row in names.names("sw")] == [first, second, ci]
    assert names.successor(first) == second
    assert names.slug_of(ci) == "sw"
    assert names.slugs_of([ci, "sw-eng-1"]) == {ci: "sw", "sw-eng-1": "sw"}
    assert names.swarm_slug("swarm@abcdef") == "sw"
    names.release("sw")
    assert names.code_of("sw") == ""
    assert (
        naming.NameRegistry(None, mint=iter(["abcdef", "123456"]).__next__).mint_code("sw", "sw", "/repo") == "123456"
    )


def test_names_written_on_redis_live_in_the_names_state_store(client, on_redis):
    names = naming.NameRegistry(client, mint=lambda: "abcdef")
    names.mint_code("sw", "sw", "/repo")
    name = names.next("sw", "eng", at=3)
    shared = RedisStateStore(client, naming.BUCKET)
    assert json.loads(shared.get(naming.state_key("name", name)).value)["spawned_at"] == 3
    assert json.loads(shared.get(naming.state_key("code-of", "sw")).value) == "abcdef"
    assert not client.exists(_legacy("name", name))
    assert not client.exists(_legacy("codes"))


def test_names_written_before_the_move_still_read_and_seed_the_first_write(client, on_redis):
    client.hset(_legacy("codes"), "abcdef", json.dumps({"swarm": "sw", "name": "swarm@abcdef"}))
    client.hset(_legacy("code-of"), "sw", "abcdef")
    client.hset(_legacy("seq", "abcdef"), "engineer", 41)
    old = "engineer@abcdef-0041"
    client.hset(_legacy("name", old), mapping={"type": "engineer", "number": 41, "spawned_at": 2, "retired_at": 0})
    client.rpush(_legacy("names", "abcdef"), "sw-eng-1", old)
    client.set(_legacy("alias", "sw-eng-1"), old)
    client.sadd(_legacy("aliases-of", old), "sw-eng-1")
    names = naming.NameRegistry(client, mint=lambda: "abcdef")

    assert names.mint_code("sw", "sw", "/repo") == "abcdef"
    assert names.resolve("sw-eng-1") == old
    assert names.entry("sw-eng-1")["spawned_at"] == 2
    fresh = names.next("sw", "eng", at=4)
    assert fresh == "engineer@abcdef-0042"
    names.retire(old, 8)
    assert names.entry(old)["retired_at"] == 8
    assert [row["name"] for row in names.names("sw")] == [old, old, fresh]
    assert names.successor(old) == fresh
    names.release("sw")
    assert names.code_of("sw") == ""


def test_a_code_held_only_before_the_move_is_never_minted_again(client, on_redis):
    client.hset(_legacy("codes"), "abcdef", json.dumps({"swarm": "other"}))
    names = naming.NameRegistry(client, mint=iter(["abcdef", "123456"]).__next__)
    assert names.mint_code("sw", "sw", "/repo") == "123456"


def test_export_and_restore_carry_the_registry_state(client, on_redis):
    import fakeredis

    store = RedisStore(client)
    store.create(SwarmConfig("sw", "/home/x/dev/agentihooks", max_eng=2, max_ci=1))
    name = store.next_name("sw", "eng", at=3)
    store.names.alias("sw-eng-1", name)
    dumped = json.loads(json.dumps(store.export("sw")))

    other = RedisStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    other.restore("sw", dumped)
    assert other.names.entry("sw-eng-1")["spawned_at"] == 3
    assert other.names.code_of("sw") == store.names.code_of("sw")
    assert [row["name"] for row in other.names.names("sw")] == [name]


def test_retiring_an_agent_records_retired_at_through_the_registry(client, on_redis):
    store = RedisStore(client)
    store.create(SwarmConfig("sw", "/home/x/dev/agentihooks", max_eng=2, max_ci=1))
    name = store.next_name("sw", "eng", at=3)
    store._retire_name(name, 11)
    assert store.names.entry(name)["retired_at"] == 11
