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
