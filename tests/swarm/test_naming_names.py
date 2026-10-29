import json

import pytest

from scripts.events import memory
from scripts.events.config import BusConfig
from scripts.events.redis_state import RedisStateStore
from scripts.swarm import name_state, naming

pytestmark = pytest.mark.unit


@pytest.fixture
def client():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


@pytest.fixture
def bus(monkeypatch):
    opened = memory.open_bus(BusConfig())
    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "bus")
    monkeypatch.setattr(name_state, "_OPENED", {})
    monkeypatch.setattr(name_state.state, "open_state", opened.state)
    yield opened
    opened.close()


@pytest.fixture
def on_redis(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "redis")


@pytest.fixture
def names(client, on_redis):
    registry = naming.NameRegistry(client, mint=lambda: "abcdef")
    registry.mint_code("sw", "sw", "/repo")
    return registry


def _legacy(*parts):
    return naming.NameRegistry.key(*parts)


def _shared(client, kind, part):
    entry = RedisStateStore(client, name_state.BUCKET).get(name_state.state_key(kind, part))
    return None if entry is None else json.loads(entry.value)


def _record(kind, number, at, retired=0, session=""):
    fields = {"type": kind, "number": number, "code": "abcdef", "swarm": "sw"}
    return {**fields, "session_id": session, "spawned_at": at, "retired_at": retired}


def _entry(name, *args, **kwargs):
    return {"name": name, **_record(*args, **kwargs)}


def test_names_run_on_the_bus_with_no_redis_client(bus):
    registry = naming.NameRegistry(None, mint=lambda: "abcdef")
    registry.mint_code("sw", "sw", "/repo")
    first = registry.next("sw", "eng", at=9)
    ci = registry.next("sw", "ci", at=3)
    second = registry.next("sw", "eng", at=5)
    assert (first, ci, second) == ("engineer@abcdef-0001", "ci@abcdef-0001", "engineer@abcdef-0002")
    registry.note(first, session_id="s1")
    registry.retire(first, 11)
    registry.retire(first, 12)
    registry.note("stranger", session_id="s2")
    assert registry.entry(first) == _entry(first, "engineer", 1, 9, retired=11, session="s1")
    assert registry.entry("stranger") == {}
    assert [row["name"] for row in registry.names("sw")] == [ci, second, first]
    assert registry.successor(first) == second
    assert registry.successor(second) == ""
    assert registry.slug_of(ci) == "sw"
    assert registry.swarm_keys("sw") == []
    registry.alias("sw-eng-1", second)
    with pytest.raises(naming.NamingError, match=f"^registered name {ci} cannot be an alias$"):
        registry.alias(ci, second)
    assert registry.names("missing") == []


def test_a_name_spawned_on_redis_lives_in_the_names_state_store(client, names):
    name = names.next("sw", "eng", at=4)
    names.note(name, session_id="s1")
    names.retire(name, 8)
    code = names.code_of("sw")
    assert _shared(client, "seq", code) == {"engineer": 1}
    assert _shared(client, "name", name) == _record("engineer", 1, 4, retired=8, session="s1")
    assert not client.exists(_legacy("seq", code), _legacy("names", code), _legacy("name", name))
    assert names.entry(name) == _entry(name, "engineer", 1, 4, retired=8, session="s1")


def test_names_written_before_the_move_still_read_and_move_on_their_first_write(client, names):
    old = "engineer@abcdef-0001"
    client.hset(_legacy("seq", "abcdef"), "engineer", 1)
    client.hset(
        _legacy("name", old),
        mapping=_record("engineer", 1, 2),
    )
    client.rpush(_legacy("names", "abcdef"), old)
    assert names.entry(old) == _entry(old, "engineer", 1, 2)
    new = names.next("sw", "eng", at=6)
    assert new == "engineer@abcdef-0002"
    assert names.successor(old) == new
    names.retire(old, 7)
    assert _shared(client, "name", old)["retired_at"] == 7
    assert names.successor(old) == new
    assert [row["name"] for row in names.names("sw")] == [old, new]
    assert names.swarm_keys("sw") == [
        _legacy("seq", "abcdef"),
        _legacy("names", "abcdef"),
        _legacy("name", old),
        _legacy("aliases-of", old),
    ]


def test_note_never_creates_a_record_for_an_unknown_name(client, names):
    names.note("engineer@abcdef-0009", session_id="s1")
    assert _shared(client, "name", "engineer@abcdef-0009") is None
    assert names.entry("engineer@abcdef-0009") == {}


def test_export_carries_the_whole_names_state_that_restore_writes_back(client, names):
    first = names.next("sw", "eng", at=1)
    ci = names.next("sw", "ci", at=2)
    names.alias("sw-eng-1", first)
    names.retire(ci, 5)
    rows = names.export("sw")
    assert rows == {
        name_state.state_key("codes", "abcdef"): names.swarm("abcdef"),
        name_state.state_key("code-of", "sw"): "abcdef",
        name_state.state_key("seq", "abcdef"): {"engineer": 1, "ci": 1},
        name_state.state_key("name", first): _record("engineer", 1, 1),
        name_state.state_key("aliases-of", first): ["sw-eng-1"],
        name_state.state_key("alias", "sw-eng-1"): first,
        name_state.state_key("name", ci): _record("ci", 1, 2, retired=5),
        name_state.state_key("aliases-of", ci): [],
    }
    client.flushall()
    names.names_state.restore(json.loads(json.dumps(rows)))
    assert names.export("sw") == rows
    assert names.entry("sw-eng-1") == _entry(first, "engineer", 1, 1)


def test_the_module_level_lookups_read_the_bus_with_no_redis(bus, monkeypatch):
    import hooks._redis

    monkeypatch.setattr(hooks._redis, "get_redis", lambda: pytest.fail("the bus backend needs no Redis client"))
    registry = naming.NameRegistry(None, mint=lambda: "abcdef")
    registry.mint_code("sw", "sw", "/repo")
    new = registry.next("sw", "eng")
    registry.alias("sw-eng-1", new)
    assert naming.resolve_name("sw-eng-1") == new
    assert naming.resolve_names(["sw-eng-1", "x"]) == {"sw-eng-1": new, "x": "x"}
    assert naming.addresses("sw-eng-1") == [new, "sw-eng-1"]
    assert naming.registered_slug("sw-eng-1") == "sw"
