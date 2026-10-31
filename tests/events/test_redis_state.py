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


def test_a_write_commits_while_other_keys_of_its_bucket_change_on_every_attempt():
    import fakeredis

    client = fakeredis.FakeRedis(decode_responses=True)
    sibling = RedisStateStore(client, "probe", prefix="fixture")
    sibling.create("other", b"0")
    raced = []

    def racing(value: bytes) -> bytes:
        entry = sibling.get("other")
        raced.append(sibling.update("other", str(len(raced) + 1).encode(), entry.revision))
        return value

    states = RedisStateStore(client, "probe", prefix="fixture", projection=racing)
    written = states.create("doc", b"mine")
    assert len(raced) == 1
    assert written > raced[-1]
    assert states.get("doc").value == b"mine"


def test_a_stale_revision_on_the_same_key_still_conflicts():
    import fakeredis

    from scripts.events.bus import StateConflict

    states = RedisStateStore(fakeredis.FakeRedis(decode_responses=True), "probe", prefix="fixture")
    first = states.create("doc", b"one")
    states.update("doc", b"two", first)
    with pytest.raises(StateConflict, match=f"^state key 'doc' is not at revision {first}$"):
        states.update("doc", b"three", first)
    assert states.get("doc").value == b"two"


@pytest.mark.parametrize("decode", [False, True])
def test_a_write_keeps_the_stored_row_history_revision_and_projection_shape(decode):
    import base64
    import json

    import fakeredis

    client = fakeredis.FakeRedis(decode_responses=decode)
    states = RedisStateStore(client, "probe", prefix="fixture", projection=lambda value: b"view:" + value)
    first = states.create("doc", b"one")
    second = states.update("doc", b"two", first)

    def text(raw: str | bytes) -> str:
        return raw.decode() if isinstance(raw, bytes) else raw

    row = json.dumps({"revision": second, "value": base64.b64encode(b"two").decode()})
    view = json.dumps({"revision": second, "value": base64.b64encode(b"view:two").decode()})
    assert text(client.hget(states._current, "doc")) == row
    assert [text(raw) for raw in client.lrange(states._history + "doc", 0, -1)] == [
        json.dumps({"revision": first, "value": base64.b64encode(b"one").decode()}),
        row,
    ]
    assert int(client.get(states._revision)) == int(client.get(states._published)) == second
    assert text(client.hget(states._projected, "doc")) == view
