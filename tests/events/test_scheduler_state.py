from unittest.mock import Mock

import pytest

from scripts.events import scheduler_state
from scripts.events.config import BusConfig
from scripts.scheduler.core import Scheduler

from . import nats_server

pytestmark = pytest.mark.unit


def test_scheduler_bus_is_shared_across_timers_and_closed_at_exit(monkeypatch):
    from scripts.events import factory

    scheduler_state._bus.cache_clear()
    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "bus")
    monkeypatch.setenv("AGENTIHOOKS_BUS", "memory")
    monkeypatch.delenv("AGENTIHOOKS_BUS_SCOPED", raising=False)
    bus = Mock()
    opened, closed = Mock(return_value=bus), Mock()
    monkeypatch.setattr(factory, "open_bus", opened)
    monkeypatch.setattr(scheduler_state, "atexit", Mock(register=closed))
    try:
        assert scheduler_state.open_scheduler_state("one") is bus.state.return_value
        assert scheduler_state.open_scheduler_state("two") is bus.state.return_value
        opened.assert_called_once_with(BusConfig.from_environ())
        assert [call.args for call in bus.state.call_args_list] == [("one",), ("two",)]
        closed.assert_called_once_with(bus.close)
    finally:
        scheduler_state._bus.cache_clear()


def test_scheduler_selects_its_swarm_on_a_scoped_bus(monkeypatch):
    from scripts.events import factory

    scheduler_state._bus.cache_clear()
    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "bus")
    monkeypatch.setenv("AGENTIHOOKS_BUS", "memory")
    monkeypatch.setenv("AGENTIHOOKS_BUS_SCOPED", "1")
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "scheduler-proof")
    bus = Mock()
    monkeypatch.setattr(factory, "open_bus", Mock(return_value=bus))
    monkeypatch.setattr(scheduler_state, "atexit", Mock())
    try:
        result = scheduler_state.open_scheduler_state("scheduler-member")
        bus.for_swarm.assert_called_once_with("scheduler-proof")
        bus.for_swarm.return_value.state.assert_called_once_with("scheduler-member")
        assert result is bus.for_swarm.return_value.state.return_value
    finally:
        scheduler_state._bus.cache_clear()


def test_scheduler_dispatches_against_real_nats_and_reuses_its_connection(tmp_path, monkeypatch):
    scheduler_state._bus.cache_clear()
    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "bus")
    monkeypatch.setenv("AGENTIHOOKS_BUS", "nats")
    monkeypatch.delenv("AGENTIHOOKS_BUS_SCOPED", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_BUS_BIND", raising=False)
    with nats_server.running(tmp_path / "jetstream") as url:
        monkeypatch.setenv("AGENTIHOOKS_BUS_URL", url)
        try:
            first, second = Scheduler(machine="nats-proof"), Scheduler(machine="nats-proof")
            assert first.state is second.state
            entry = first.timer(owner="proof", payload="ring", delay=1, now=10)
            entry.start(now=10)
            assert second.run_due(now=11) == 1
            assert entry.state == "fired"
            assert second.run_due(now=12) == 0
        finally:
            scheduler_state._bus(BusConfig.from_environ()).close()
            scheduler_state._bus.cache_clear()


class _Store:
    def __init__(self):
        self.rows, self.revision = {}, 0

    def get(self, key):
        return self.rows.get(key)

    def create(self, key, value):
        from scripts.events.bus import StateConflict, StateEntry

        if key in self.rows:
            raise StateConflict(f"{key} exists")
        self.revision += 1
        self.rows[key] = StateEntry(key=key, value=value, revision=self.revision)
        return self.revision


@pytest.fixture
def redis_state():
    import fakeredis

    from scripts.events.redis_state import RedisStateStore

    return RedisStateStore(fakeredis.FakeRedis(decode_responses=True), "scheduler", prefix="proof")


def _document(state, key="entries"):
    import json

    return json.loads(state.get(key).value)


def test_the_redis_backend_opens_the_named_bucket(monkeypatch):
    from scripts.events import redis_state
    from scripts.swarm import store

    client, opened = Mock(), Mock()
    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "redis")
    monkeypatch.setattr(store, "redis_client", lambda: client)
    monkeypatch.setattr(redis_state, "RedisStateStore", opened)
    assert scheduler_state.open_scheduler_state("scheduler-member") is opened.return_value
    opened.assert_called_once_with(client, "scheduler-member")


def test_a_bus_store_is_read_without_a_legacy_lookup():
    store = _Store()
    store.create("entries", b'{"lease": {}}')
    assert scheduler_state.read(store, "entries", "legacy:entries") is store.rows["entries"]


def test_legacy_entries_and_retired_ids_migrate_into_a_new_document(redis_state):
    redis_state.redis.hset("legacy:entries", mapping={"a": '{"state": "active"}'})
    redis_state.redis.hset("legacy:retired", mapping={"b": "1"})
    stored = scheduler_state.read(redis_state, "entries", "legacy:entries")
    assert stored == redis_state.get("entries")
    assert _document(redis_state) == {"entries": {"a": {"state": "active"}}, "retired": {"b": True}}


def test_only_retired_legacy_ids_still_migrate(redis_state):
    redis_state.redis.hset("legacy:retired", mapping={"b": "1"})
    scheduler_state.read(redis_state, "entries", "legacy:entries")
    assert _document(redis_state) == {"entries": {}, "retired": {"b": True}}


def test_a_document_without_entries_keeps_its_fields_when_legacy_entries_merge(redis_state):
    redis_state.create("entries", b'{"lease": {"token": "t"}}')
    redis_state.redis.hset("legacy:entries", mapping={"a": '{"state": "active"}'})
    stored = scheduler_state.read(redis_state, "entries", "legacy:entries")
    assert stored.revision == 2
    assert _document(redis_state) == {"lease": {"token": "t"}, "entries": {"a": {"state": "active"}}, "retired": {}}


def test_a_migrated_document_is_returned_without_reading_legacy_again(redis_state):
    redis_state.create("entries", b'{"entries": {}}')
    redis_state.redis.hset("legacy:entries", mapping={"a": '{"state": "active"}'})
    stored = scheduler_state.read(redis_state, "entries", "legacy:entries")
    assert (stored.revision, _document(redis_state)) == (1, {"entries": {}})


@pytest.mark.parametrize("stored", [None, b"", b'{"lease": {}}'], ids=["missing", "empty", "lease"])
def test_no_legacy_rows_leaves_the_document_as_it_was(redis_state, stored):
    if stored is not None:
        redis_state.create("entries", stored)
    before = redis_state.get("entries")
    assert scheduler_state.read(redis_state, "entries", "legacy:entries") == before
    assert redis_state.get("entries") == before


def test_a_conflicting_migration_retries_on_the_new_revision(redis_state, monkeypatch):
    from scripts.events.bus import StateConflict

    redis_state.redis.hset("legacy:entries", mapping={"a": '{"state": "active"}'})
    create = redis_state.create

    def racing(key, value):
        monkeypatch.setattr(redis_state, "create", create)
        create(key, b'{"lease": {}}')
        raise StateConflict("raced")

    monkeypatch.setattr(redis_state, "create", racing)
    scheduler_state.read(redis_state, "entries", "legacy:entries")
    assert _document(redis_state) == {"lease": {}, "entries": {"a": {"state": "active"}}, "retired": {}}


def test_a_migration_that_never_settles_is_refused(redis_state, monkeypatch):
    from scripts.events.bus import StateConflict
    from scripts.events.redis_state import WRITE_TRIES

    redis_state.redis.hset("legacy:entries", mapping={"a": '{"state": "active"}'})
    attempts = []

    def conflict(key, value):
        attempts.append(key)
        raise StateConflict("raced")

    monkeypatch.setattr(redis_state, "create", conflict)
    with pytest.raises(StateConflict) as refused:
        scheduler_state.read(redis_state, "entries", "legacy:entries")
    assert str(refused.value) == "scheduler migration 'entries' kept changing"
    assert attempts == ["entries"] * WRITE_TRIES


def test_a_redis_wake_keeps_only_the_latest_delivery(redis_state):
    redis_state.redis.rpush("wake:key", "s1", "s2", "s3")
    scheduler_state.wake(redis_state, "wake:key", "d0")
    assert redis_state.redis.lrange("wake:key", 0, -1) == ["d0"]
    for delivery in ("d1", "d2", "d3"):
        scheduler_state.wake(redis_state, "wake:key", delivery)
    assert redis_state.redis.lrange("wake:key", 0, -1) == ["d3"]


def test_a_bus_wake_records_the_first_delivery_under_a_dotted_key():
    store = _Store()
    scheduler_state.wake(store, "wake:key", "d1")
    scheduler_state.wake(store, "wake:key", "d2")
    assert list(store.rows) == ["wake.key"]
    assert store.rows["wake.key"].value == b"d1"
