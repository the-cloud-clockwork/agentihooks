import hashlib
import json

import pytest

from scripts.events import memory
from scripts.events.bus import StateConflict, StateEntry
from scripts.events.config import BusConfig
from scripts.events.redis_state import RedisStateStore
from scripts.swarm import name_state
from scripts.swarm.name_state import KEEP, MISSING, NameState, NamingError, state_key

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


def _never(*_):
    raise AssertionError("legacy read on a key the store holds")


class Racing:
    def __init__(self, inner, races):
        self.inner, self.races = inner, races

    def get(self, key):
        return self.inner.get(key)

    def create(self, key, value):
        if self.races:
            self.races -= 1
            raise StateConflict(key)
        return self.inner.create(key, value)

    def update(self, key, value, revision):
        return self.create(key, value) if self.races else self.inner.update(key, value, revision)


def test_a_state_key_is_its_kind_and_the_digest_of_its_part():
    assert state_key("alias", "sw-eng-1") == "alias." + hashlib.sha256(b"sw-eng-1").hexdigest()


def test_value_tells_a_missing_key_from_a_removed_one():
    assert name_state.value(None) is MISSING
    assert name_state.value(StateEntry("k", b"", 1)) is None
    assert name_state.value(StateEntry("k", b'{"a": 1}', 1)) == {"a": 1}


def test_row_makes_the_whole_number_fields_integers():
    raw = {"type": "engineer", "number": "4", "spawned_at": "5", "retired_at": "0", "session_id": "7"}
    assert name_state.row(raw) == {"type": "engineer", "number": 4, "spawned_at": 5, "retired_at": 0, "session_id": "7"}
    assert name_state.row({}) is None


def test_read_many_falls_back_to_one_read_per_key():
    class Single:
        def get(self, key):
            return StateEntry(key, b"1", 1) if key == "a" else None

    assert name_state.read_many(Single(), ["a", "b"]) == [StateEntry("a", b"1", 1), None]


def test_on_redis_the_store_is_the_names_hash_of_the_same_client(client, on_redis):
    shared = name_state.shared_store(client)
    assert isinstance(shared, RedisStateStore) and shared.redis is client
    shared.create("k", b"1")
    assert RedisStateStore(client, "names").get("k").value == b"1"


def test_on_the_bus_the_names_bucket_opens_once(bus):
    first = name_state.shared_store(None)
    assert name_state.shared_store(None) is first
    assert name_state._OPENED == {"names": first}


def test_writes_create_update_keep_and_remove(bus):
    names = NameState(None)
    key = state_key("seq", "abcdef")
    assert names.write(key, lambda current: {"engineer": 1} if current is None else KEEP, _never) == {"engineer": 1}
    assert names.write(key, lambda seq: {**seq, "ci": 1}, _never) == {"engineer": 1, "ci": 1}
    assert names.write(key, lambda _: KEEP, _never) == {"engineer": 1, "ci": 1}
    assert names.get("seq", "abcdef", _never) == {"engineer": 1, "ci": 1}
    assert names.amend("seq", "abcdef", lambda _: None, _never) is None
    assert names.shared.get(key).value == b""
    assert names.get("seq", "abcdef", _never) is None
    assert names.write(state_key("seq", "other"), lambda _: None, _never) is None
    assert names.shared.get(state_key("seq", "other")) is None


def test_the_bus_never_reads_legacy_keys(bus):
    names = NameState(None)
    assert names.legacy is None
    assert names.get("alias", "x", _never) is None
    assert names.get_many("alias", ["x", "y"], _never) == [None, None]
    assert names.write(state_key("alias", "x"), lambda current: current or "y", _never) == "y"
    assert names.watched("legacy") == ["legacy"]


def test_a_raced_write_reads_again_and_lands(bus):
    names = NameState(None)
    names.shared = Racing(names.shared, 3)
    assert names.write("k", lambda current: (current or 0) + 1, _never) == 1
    assert json.loads(names.shared.get("k").value) == 1


def test_a_key_that_keeps_changing_is_refused(bus):
    names = NameState(None)
    names.shared = Racing(names.shared, name_state.WRITE_TRIES)
    with pytest.raises(NamingError, match="name registry key k kept changing"):
        names.write("k", lambda _: 1, _never)
    names.shared.races = name_state.WRITE_TRIES - 1
    assert names.write("k", lambda _: 2, _never) == 2


def test_legacy_values_read_until_the_first_write_moves_them(client, on_redis):
    client.set("old:alias", "engineer@abcdef-0001")
    names = NameState(client)
    assert names.legacy is client

    def legacy(redis):
        return redis.get("old:alias")

    assert names.get("alias", "x", legacy) == "engineer@abcdef-0001"
    assert names.amend("alias", "x", lambda held: KEEP if held else "other", legacy) == "engineer@abcdef-0001"
    assert names.shared.get(state_key("alias", "x")) is None
    assert names.amend("alias", "x", lambda held: held + "!", legacy) == "engineer@abcdef-0001!"
    client.delete("old:alias")
    assert names.get("alias", "x", legacy) == "engineer@abcdef-0001!"


def test_many_reads_ask_legacy_once_for_the_parts_the_store_lacks(client, on_redis):
    names = NameState(client)
    names.amend("alias", "held", lambda _: "a", lambda _: None)
    asked = []

    def legacy(redis, missing):
        asked.append(missing)
        return [f"old-{part}" for part in missing]

    assert names.get_many("alias", ["held", "x", "y", "x"], legacy) == ["a", "old-x", "old-y", "old-x"]
    assert asked == [["x", "y"]]
    assert names.get_many("alias", ["held"], legacy) == ["a"]
    assert asked == [["x", "y"]]


def test_a_reader_reads_the_store_and_legacy_keys_through_itself(client, on_redis):
    names = NameState(client)
    names.amend("alias", "x", lambda _: "a", lambda _: None)
    with client.pipeline() as pipe:
        pipe.watch("watched")
        shared, legacy = names.views(pipe)
        assert isinstance(shared, RedisStateStore) and shared.redis is pipe and legacy is pipe
        assert names.get("alias", "x", _never, pipe) == "a"
        assert names.get("alias", "y", lambda redis: redis is pipe, pipe) is True
    assert names.views(None) == (names.shared, client)


def test_on_redis_a_transaction_watches_the_legacy_key_and_the_store(client, on_redis):
    names = NameState(client)
    assert names.watched("old:alias") == ["old:alias", names.shared.watch_key]
    assert names.watched("old:alias")[1] == RedisStateStore(client, "names")._current


def test_restore_writes_each_row_over_what_the_store_holds(bus):
    names = NameState(None)
    names.amend("alias", "x", lambda _: "a", _never)
    names.restore({state_key("alias", "x"): "b", state_key("seq", "c"): {"engineer": 2}})
    assert names.get("alias", "x", _never) == "b"
    assert names.get("seq", "c", _never) == {"engineer": 2}


def test_redis_state_reads_many_keys_in_one_round_trip(client):
    store = RedisStateStore(client, "names")
    store.create("a", b"1")
    assert store.get_many([]) == []
    assert store.get_many(["a", "b"]) == [store.get("a"), None]
    assert store.watch_key == store._current
    assert client.hget(store.watch_key, "a") is not None
