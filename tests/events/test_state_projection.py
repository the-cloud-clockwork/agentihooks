import base64
import json

import pytest

from scripts.events.bus import StateConflict, StateEntry
from scripts.events.redis_state import RedisStateStore

pytestmark = pytest.mark.unit


def project(value: bytes) -> bytes:
    return b"view:" + value


def present(entry: StateEntry | None) -> StateEntry:
    assert entry is not None
    return entry


def projected(states: RedisStateStore, key: str) -> StateEntry:
    return present(states.get_projection(key))


def test_projection_and_document_commit_at_the_same_revision() -> None:
    from fakeredis import FakeRedis

    states = RedisStateStore(FakeRedis(decode_responses=True), "probe", projection=project)
    first = states.create("one", b"first")
    assert present(states.get("one")).value == b"first"
    assert projected(states, "one").value == b"view:first"
    assert projected(states, "one").revision == first
    assert projected(states, "one").key == "one"
    second = states.update("one", b"second", first)
    assert present(states.get("one")).value == b"second"
    assert projected(states, "one").value == b"view:second"
    assert projected(states, "one").revision == second
    with pytest.raises(StateConflict):
        states.update("one", b"refused", first)
    assert projected(states, "one").value == b"view:second"
    assert states.get_projection("missing") is None


def test_an_unprojected_write_invalidates_only_its_own_projection() -> None:
    from fakeredis import FakeRedis

    redis = FakeRedis(decode_responses=True)
    states = RedisStateStore(redis, "probe", projection=project)
    first = states.create("one", b"first")
    states.create("two", b"other")
    RedisStateStore(redis, "probe").update("one", b"new", first)
    assert states.get_projection("one") is None
    assert projected(states, "two").value == b"view:other"
    assert present(states.get("one")).value == b"new"


def test_an_older_writer_invalidates_views_until_a_new_writer_clears_them() -> None:
    from fakeredis import FakeRedis

    redis = FakeRedis(decode_responses=True)
    states = RedisStateStore(redis, "probe", projection=project)
    states.create("one", b"first")
    other = states.create("two", b"other")
    assert projected(states, "one").value == b"view:first"
    assert projected(states, "two").value == b"view:other"
    revision = redis.incr(states._revision)
    row = json.dumps({"revision": revision, "value": base64.b64encode(b"old writer").decode()})
    redis.hset(states._current, "one", row)
    assert states.get_projection("one") is None
    assert states.get_projection("two") is None
    states.update("two", b"fresh", other)
    assert states.get_projection("one") is None
    assert projected(states, "two").value == b"view:fresh"
    assert present(states.get("one")).value == b"old writer"
