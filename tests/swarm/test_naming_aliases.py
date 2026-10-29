import json
import re

import pytest

from scripts.events import memory
from scripts.events.bus import StateConflict
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
def agent(client, on_redis):
    names = naming.NameRegistry(client, mint=lambda: "abcdef")
    names.mint_code("sw", "sw", "/repo")
    return names, names.next("sw", "eng")


def _legacy(*parts):
    return naming.NameRegistry.key(*parts)


def _shared(client, kind, part):
    entry = RedisStateStore(client, name_state.BUCKET).get(name_state.state_key(kind, part))
    return None if entry is None else json.loads(entry.value)


def test_aliases_read_on_the_bus_with_no_redis_client(bus):
    names = naming.NameRegistry(None)
    names.names_state.amend("alias", "old", lambda _: "engineer@abcdef-0001", lambda _: None)
    names.names_state.amend("aliases-of", "engineer@abcdef-0001", lambda _: ["old"], lambda _: None)
    assert names.resolve("old") == "engineer@abcdef-0001"
    assert names.resolve("stranger") == "stranger"
    assert names.resolve_many(["old", "stranger"]) == {"old": "engineer@abcdef-0001", "stranger": "stranger"}
    assert names.aliases("old") == ["old"]
    assert names.aliases("stranger") == []


def test_an_alias_written_on_redis_lives_in_the_names_state_store(client, agent):
    names, new = agent
    names.alias("sw-eng-1", new)
    names.alias("sw-eng-1", new)
    assert _shared(client, "alias", "sw-eng-1") == new
    assert _shared(client, "aliases-of", new) == ["sw-eng-1"]
    assert not client.exists(_legacy("alias", "sw-eng-1"))
    assert not client.exists(_legacy("aliases-of", new))
    assert names.resolve("sw-eng-1") == new
    assert names.aliases("sw-eng-1") == ["sw-eng-1"]


def test_aliases_written_before_the_move_still_read_and_a_new_alias_keeps_them(client, agent):
    names, new = agent
    client.set(_legacy("alias", "sw-eng-1"), new)
    client.sadd(_legacy("aliases-of", new), "sw-eng-1")
    assert names.resolve("sw-eng-1") == new
    assert names.resolve_many(["sw-eng-1", "stranger"]) == {"sw-eng-1": new, "stranger": "stranger"}
    assert names.aliases(new) == ["sw-eng-1"]
    names.alias("sw-eng-1", new)
    assert _shared(client, "alias", "sw-eng-1") is None
    names.alias("sw-eng-2", new)
    assert names.aliases(new) == ["sw-eng-1", "sw-eng-2"]
    assert _shared(client, "aliases-of", new) == ["sw-eng-1", "sw-eng-2"]


def test_an_alias_held_before_the_move_is_never_pointed_elsewhere(client, agent):
    names, new = agent
    other = names.next("sw", "eng")
    client.set(_legacy("alias", "sw-eng-1"), new)
    with pytest.raises(naming.NamingError, match=f"^{re.escape(f'alias sw-eng-1 already belongs to {new}')}$"):
        names.alias("sw-eng-1", other)
    assert _shared(client, "aliases-of", other) is None


def test_an_alias_lost_to_a_racing_owner_leaves_no_membership_behind(client, agent):
    names, new = agent
    other = names.next("sw", "eng")
    racing = naming.NameRegistry(client)
    real = names.names_state.amend

    def amend(kind, part, change, legacy):
        if kind == "alias":
            racing.alias("sw-eng-1", other)
        return real(kind, part, change, legacy)

    names.names_state.amend = amend
    with pytest.raises(naming.NamingError, match=f"^{re.escape(f'alias sw-eng-1 already belongs to {other}')}$"):
        names.alias("sw-eng-1", new)
    names.names_state.amend = real
    assert names.aliases(new) == []
    assert names.aliases(other) == ["sw-eng-1"]
    assert names.resolve("sw-eng-1") == other


def test_a_registered_name_or_an_unknown_agent_cannot_take_an_alias(client, agent):
    names, new = agent
    other = names.next("sw", "eng")
    with pytest.raises(naming.NamingError, match=f"^{re.escape(f'registered name {other} cannot be an alias')}$"):
        names.alias(other, new)
    with pytest.raises(naming.NamingError, match="^no registered agent stranger$"):
        names.alias("sw-eng-1", "stranger")
    assert _shared(client, "aliases-of", new) is None


def test_a_reader_sees_aliases_in_the_store_and_before_the_move(client, agent):
    names, new = agent
    names.alias("sw-eng-1", new)
    client.set(_legacy("alias", "sw-eng-2"), new)
    with client.pipeline() as pipe:
        pipe.watch(*names.watched("alias", "sw-eng-1"))
        assert names.resolve("sw-eng-1", pipe) == new
        assert names.resolve("sw-eng-2", pipe) == new
        assert names.aliases("sw-eng-1", pipe) == ["sw-eng-1"]


def test_watched_names_the_legacy_key_and_the_store_on_redis(client, on_redis):
    names = naming.NameRegistry(client)
    shared = RedisStateStore(client, name_state.BUCKET).watch_key
    assert names.watched("alias", "x") == [_legacy("alias", "x"), shared]
    assert names.watched("aliases-of", "y") == [_legacy("aliases-of", "y"), shared]


def test_watched_is_the_legacy_key_alone_on_the_bus(bus):
    assert naming.NameRegistry(None).watched("alias", "x") == [_legacy("alias", "x")]


@pytest.mark.parametrize("error", ["redis", OSError, StateConflict])
def test_resolve_keeps_the_name_when_the_store_fails(client, on_redis, monkeypatch, caplog, error):
    from redis.exceptions import RedisError

    names = naming.NameRegistry(client)
    raised = RedisError if error == "redis" else error

    def down(*_):
        raise raised("down")

    monkeypatch.setattr(names.names_state.shared, "get", down)
    monkeypatch.setattr(names.names_state.shared, "get_many", down)
    assert names.resolve("old") == "old"
    assert names.resolve_many(["a", "b"]) == {"a": "a", "b": "b"}
    assert [(r.name, r.levelname, r.getMessage()) for r in caplog.records] == [
        ("scripts.swarm.naming", "WARNING", "alias lookup failed for old: down"),
        ("scripts.swarm.naming", "WARNING", "alias lookup failed for 2 names: down"),
    ]


def test_resolve_keeps_the_name_when_the_bus_store_fails(bus, monkeypatch, caplog):
    names = naming.NameRegistry(None)

    def down(*_):
        raise RuntimeError("down")

    monkeypatch.setattr(names.names_state.shared, "get", down)
    assert names.resolve("old") == "old"
    assert [r.getMessage() for r in caplog.records] == ["alias lookup failed for old: down"]


def test_a_registered_slug_raises_a_failed_store_read(client, on_redis, monkeypatch):
    from hooks import _redis

    failure = OSError("store read timed out")

    def down(*_):
        raise failure

    monkeypatch.setattr(_redis, "get_redis", lambda: client)
    monkeypatch.setattr(RedisStateStore, "get", down)
    monkeypatch.setattr(naming.NameRegistry, "slug_of", lambda self, name: pytest.fail("slug_of swallows the failure"))
    with pytest.raises(OSError) as raised:
        naming.registered_slug("master@abcdef-0001")
    assert raised.value is failure


def test_export_lists_the_swarm_aliases_as_store_rows_that_restore_writes_back(client, agent):
    names, new = agent
    quiet = names.next("sw", "eng")
    names.alias("sw-eng-1", new)
    client.set(_legacy("alias", "sw-eng-2"), new)
    client.sadd(_legacy("aliases-of", new), "sw-eng-2")
    others = naming.NameRegistry(client, mint=lambda: "123456")
    others.mint_code("other", "other", "/repo")
    others.alias("other-eng-1", others.next("other", "eng"))
    rows = names.export("sw")
    assert rows == {
        name_state.state_key("aliases-of", new): ["sw-eng-1", "sw-eng-2"],
        name_state.state_key("alias", "sw-eng-1"): new,
        name_state.state_key("alias", "sw-eng-2"): new,
    }
    assert name_state.state_key("aliases-of", quiet) not in rows
    assert names.export("missing") == {}
    client.delete(_legacy("alias", "sw-eng-2"), _legacy("aliases-of", new))
    client.delete(RedisStateStore(client, name_state.BUCKET).watch_key)
    names.names_state.restore(json.loads(json.dumps(rows)))
    assert names.resolve("sw-eng-2") == new
    assert names.aliases(new) == ["sw-eng-1", "sw-eng-2"]
