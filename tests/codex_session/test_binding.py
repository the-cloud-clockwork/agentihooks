import json
from dataclasses import asdict, replace

import pytest

from scripts.codex_session.binding import (
    CONNECTED,
    ENDPOINTS,
    OPENINGS,
    RECORDS,
    SCHEMA_VERSION,
    STARTING,
    TRANSPORT,
    BindingError,
    BindingStore,
    TransportRecord,
    decode,
)
from scripts.swarm.store import RedisStore, SwarmConfig

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

SLUG = "fixture"
EXACT = {
    "unsupported transport record version": "^unsupported transport record version$",
    "duplicate launch": "^duplicate launch: the execution already has a transport record$",
    "endpoint already belongs to another execution": "^the endpoint already belongs to another execution$",
    "concurrent launch": "^a concurrent launch changed the transport records; this launch was refused$",
    "identity is immutable": "^transport record identity is immutable$",
    "no transport record": "^no transport record for this execution$",
}


@pytest.fixture
def bindings():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, "agentihooks", 1, 0))
    return BindingStore(store)


def record(execution="exe-one", endpoint="unix:///run/cx/one/s", **changes):
    base = TransportRecord(execution, 1, f"eng-1@{SLUG}", "engineer@abc-0001", "task", endpoint)
    return replace(base, **changes)


def test_a_reserved_record_is_versioned_and_keyed_by_swarm_and_execution(bindings):
    reserved = bindings.reserve(SLUG, record(state=CONNECTED))
    assert (reserved.state, reserved.schema_version, reserved.transport) == (STARTING, SCHEMA_VERSION, TRANSPORT)
    raw = bindings.redis.hget(bindings.store.key(SLUG, RECORDS), "exe-one")
    assert json.loads(raw) == asdict(reserved)
    assert bindings.get(SLUG, "exe-one") == reserved
    assert bindings.get("other", "exe-one") is None
    assert bindings.redis.hget(bindings.store.key(SLUG, ENDPOINTS), reserved.endpoint_ref) == "exe-one"


def test_a_record_of_another_version_is_refused():
    with pytest.raises(BindingError, match=EXACT["unsupported transport record version"]):
        decode(json.dumps({**asdict(record()), "schema_version": SCHEMA_VERSION + 1}))


def test_a_second_launch_of_one_execution_is_refused(bindings):
    first = bindings.reserve(SLUG, record())
    with pytest.raises(BindingError, match=EXACT["duplicate launch"]):
        bindings.reserve(SLUG, record(endpoint="unix:///run/cx/two/s"))
    assert bindings.get(SLUG, "exe-one") == first
    assert bindings.redis.hget(bindings.store.key(SLUG, ENDPOINTS), "unix:///run/cx/two/s") is None


def test_two_executions_cannot_share_one_endpoint(bindings):
    bindings.reserve(SLUG, record())
    with pytest.raises(BindingError, match=EXACT["endpoint already belongs to another execution"]):
        bindings.reserve(SLUG, record(execution="exe-two"))
    assert bindings.get(SLUG, "exe-two") is None


def test_a_concurrent_change_refuses_the_reservation(bindings, monkeypatch):
    from redis.exceptions import WatchError

    def interrupted(self):
        raise WatchError("changed")

    monkeypatch.setattr(type(bindings.redis.pipeline()), "execute", interrupted)
    with pytest.raises(BindingError, match=EXACT["concurrent launch"]):
        bindings.reserve(SLUG, record())


@pytest.mark.parametrize("watched", [RECORDS, ENDPOINTS])
def test_a_change_to_either_watched_key_during_the_reservation_refuses_it(bindings, monkeypatch, watched):
    pipeline = type(bindings.redis.pipeline())
    checked = pipeline.hget

    def changed_meanwhile(self, name, key):
        bindings.redis.hset(bindings.store.key(SLUG, watched), "intruder", "exe-other")
        return checked(self, name, key)

    monkeypatch.setattr(pipeline, "hget", changed_meanwhile)
    with pytest.raises(BindingError, match=EXACT["concurrent launch"]):
        bindings.reserve(SLUG, record())
    assert bindings.redis.hget(bindings.store.key(SLUG, RECORDS), "exe-one") is None


def test_updates_keep_the_record_identity(bindings):
    bindings.reserve(SLUG, record())
    bindings.put(SLUG, record(state=CONNECTED, thread_id="thread-1"))
    assert bindings.get(SLUG, "exe-one").thread_id == "thread-1"
    for field, value in (("endpoint_ref", "unix:///other"), ("execution_generation", 2), ("seat", "eng-2@fixture")):
        with pytest.raises(BindingError, match=EXACT["identity is immutable"]):
            bindings.put(SLUG, record(**{field: value}))
    with pytest.raises(BindingError, match=EXACT["no transport record"]):
        bindings.put(SLUG, record(execution="exe-missing"))


def test_release_frees_only_the_records_own_endpoint(bindings):
    first = bindings.reserve(SLUG, record())
    bindings.release(SLUG, record(execution="exe-two"))
    assert bindings.redis.hget(bindings.store.key(SLUG, ENDPOINTS), first.endpoint_ref) == "exe-one"
    bindings.release(SLUG, first)
    assert bindings.redis.hgetall(bindings.store.key(SLUG, ENDPOINTS)) == {}
    bindings.reserve(SLUG, record(execution="exe-two"))


def test_the_opening_prompt_is_claimed_once_per_execution(bindings):
    assert bindings.claim_opening(SLUG, "exe-one", 100)
    assert not bindings.claim_opening(SLUG, "exe-one", 200)
    assert bindings.claim_opening(SLUG, "exe-two", 300)
    assert bindings.redis.hget(bindings.store.key(SLUG, OPENINGS), "exe-one") == "100"
