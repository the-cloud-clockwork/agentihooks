from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from scripts.events.redis_state import RedisStateStore

from scripts.events import memory, state
from scripts.events.bus import StateConflict, StateStore
from scripts.events.config import BusConfig

from . import nats_server

pytestmark = pytest.mark.unit


@contextmanager
def _redis(tmp_path) -> Iterator[StateStore]:
    import fakeredis

    yield RedisStateStore(fakeredis.FakeRedis(decode_responses=True), "work")


@contextmanager
def _memory(tmp_path) -> Iterator[StateStore]:
    bus = memory.open_bus(BusConfig())
    try:
        yield bus.state("work")
    finally:
        bus.close()


@contextmanager
def _nats(tmp_path) -> Iterator[StateStore]:
    from scripts.events import nats

    with nats_server.running(tmp_path / "jetstream") as url:
        bus = nats.open_bus(BusConfig(backend="nats", url=url))
        try:
            yield bus.state("work")
        finally:
            bus.close()


@pytest.fixture(params=[_redis, _memory, _nats], ids=["redis", "memory", "nats"])
def store(request, tmp_path):
    with request.param(tmp_path) as opened:
        yield opened


def test_every_state_store_keeps_the_protocol(store):
    assert isinstance(store, StateStore)


def test_a_missing_key_reads_none_and_a_created_key_reads_its_value(store):
    assert store.get("a.key") is None
    revision = store.create("a.key", b"one")
    entry = store.get("a.key")
    assert (entry.key, entry.value, entry.revision) == ("a.key", b"one", revision)
    assert store.get("other") is None


def test_create_refuses_an_existing_key_and_leaves_its_value(store):
    store.create("a.key", b"one")
    with pytest.raises(StateConflict):
        store.create("a.key", b"two")
    assert store.get("a.key").value == b"one"


def test_update_takes_only_the_current_revision(store):
    first = store.create("a.key", b"one")
    second = store.update("a.key", b"two", first)
    assert second > first
    assert store.get("a.key").value == b"two"
    with pytest.raises(StateConflict):
        store.update("a.key", b"stale", first)
    with pytest.raises(StateConflict):
        store.update("missing", b"none", first)
    assert store.get("a.key").value == b"two"
    assert store.get("missing") is None


def test_values_keep_every_byte(store):
    raw = bytes(range(256))
    store.create("raw", raw)
    assert store.get("raw").value == raw
    store.create("empty", b"")
    assert store.get("empty").value == b""


def test_watch_replays_one_key_from_a_revision(store):
    first = store.create("a.key", b"one")
    store.create("b.key", b"other")
    second = store.update("a.key", b"two", first)
    assert [(e.key, e.value, e.revision) for e in store.watch("a.key", first)] == [
        ("a.key", b"one", first),
        ("a.key", b"two", second),
    ]
    assert [e.value for e in store.watch("a.key", second)] == [b"two"]
    assert list(store.watch("missing", 1)) == []


def test_redis_state_keeps_buckets_apart():
    import fakeredis

    client = fakeredis.FakeRedis(decode_responses=True)
    RedisStateStore(client, "one").create("key", b"first")
    assert RedisStateStore(client, "two").get("key") is None
    assert RedisStateStore(client, "one").get("key").value == b"first"


def test_redis_state_history_keeps_the_newest_revisions():
    import fakeredis

    store = RedisStateStore(fakeredis.FakeRedis(decode_responses=True), "work")
    revision = store.create("key", b"0")
    for value in range(1, 70):
        revision = store.update("key", str(value).encode(), revision)
    from scripts.events.redis_state import HISTORY

    values = [entry.value for entry in store.watch("key", 0)]
    assert len(values) == HISTORY
    assert values[-1] == b"69"


def test_redis_state_retries_a_write_raced_on_another_key(monkeypatch):
    import fakeredis

    client = fakeredis.FakeRedis(decode_responses=True)
    store = RedisStateStore(client, "work")
    other = RedisStateStore(client, "work")
    raced = []
    incr = client.incr

    def racing(name, amount=1):
        if not raced:
            raced.append(other.create("other", b"raced"))
        return incr(name, amount)

    monkeypatch.setattr(client, "incr", racing)
    revision = store.create("key", b"mine")
    assert raced and store.get("key").value == b"mine"
    assert store.get("key").revision == revision
    assert store.get("other").value == b"raced"


def test_the_state_backend_defaults_to_redis(monkeypatch):
    import fakeredis

    from scripts.swarm import store as swarm_store

    client = fakeredis.FakeRedis(decode_responses=True)
    seen = []
    monkeypatch.setattr(swarm_store, "redis_client", lambda environ=None: seen.append(environ) or client)
    environ = {}
    opened = state.open_state("work", environ)
    assert isinstance(opened, RedisStateStore)
    assert seen == [environ]
    opened.create("key", b"value")
    assert RedisStateStore(client, "work").get("key").value == b"value"
    assert state.backend({}) == "redis"
    assert state.backend({state.BACKEND_ENV: "redis"}) == "redis"


def test_the_bus_state_backend_opens_the_configured_bus():
    environ = {state.BACKEND_ENV: "bus", "AGENTIHOOKS_BUS": "memory"}
    opened = state.open_state("work", environ)
    assert isinstance(opened, memory.MemoryStateStore)
    assert state.backend(environ) == "bus"


def test_the_bus_state_backend_reaches_nats(tmp_path):
    from scripts.events import nats

    with nats_server.running(tmp_path / "jetstream") as url:
        environ = {state.BACKEND_ENV: "bus", "AGENTIHOOKS_BUS": "nats", "AGENTIHOOKS_BUS_URL": url}
        opened = state.open_state("words", environ)
        assert isinstance(opened, nats.NatsStateStore)
        opened.create("key", b"value")
        assert opened.get("key").value == b"value"


def test_an_unknown_state_backend_is_refused():
    with pytest.raises(ValueError, match="unknown state backend 'disk'; known: bus, redis"):
        state.open_state("work", {state.BACKEND_ENV: "disk"})
    with pytest.raises(ValueError, match="unknown state backend 'disk'; known: bus, redis"):
        state.backend({state.BACKEND_ENV: "disk"})


def test_unavailable_names_the_backend_errors():
    import nats.errors
    import redis

    assert redis.RedisError in state.unavailable({})
    assert StateConflict in state.unavailable({})
    assert OSError in state.unavailable({})
    assert nats.errors.Error in state.unavailable({state.BACKEND_ENV: "bus"})
    assert StateConflict in state.unavailable({state.BACKEND_ENV: "bus"})
    assert RuntimeError in state.unavailable({state.BACKEND_ENV: "bus"})
    assert redis.RedisError not in state.unavailable({state.BACKEND_ENV: "bus"})
