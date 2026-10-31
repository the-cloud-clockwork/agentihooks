import pytest

from scripts.events.redis_state import RedisStateStore

pytestmark = pytest.mark.unit


def test_a_guarded_transaction_aborts_when_the_bucket_changes():
    import fakeredis
    from redis.exceptions import WatchError

    client = fakeredis.FakeRedis(decode_responses=True)
    states = RedisStateStore(client, "probe", prefix="fixture")
    states.create("doc", b"1")
    with client.pipeline() as pipe:
        states.guard(pipe)
        entry = states.get("doc")
        states.update("doc", b"2", entry.revision)
        pipe.multi()
        pipe.set("probe", "1")
        with pytest.raises(WatchError):
            pipe.execute()
    assert client.get("probe") is None


def test_writes_keep_current_and_only_one_prior_revision() -> None:
    import base64
    import json

    import fakeredis

    from scripts.events.bus import StateConflict

    client = fakeredis.FakeRedis(decode_responses=True)
    states = RedisStateStore(client, "probe", prefix="fixture")
    revision = states.create("doc", b"first")
    revision = states.update("doc", b"second", revision)
    revision = states.update("doc", b"third", revision)
    assert [(row.revision, row.value) for row in states.watch("doc")] == [(2, b"second"), (3, b"third")]
    assert [(row.revision, row.value) for row in states.watch("doc", 3)] == [(3, b"third")]
    expected = {"revision": 3, "value": base64.b64encode(b"third").decode()}
    assert json.loads(client.hget("fixture:state:probe:current", "doc")) == expected
    assert json.loads(client.lindex("fixture:state:probe:history:doc", -1)) == expected
    with pytest.raises(StateConflict):
        states.update("doc", b"conflict", 1)
    assert [(row.revision, row.value) for row in states.watch("doc")] == [(2, b"second"), (3, b"third")]
    assert states.get("doc").value == b"third"
