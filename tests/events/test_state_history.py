import pytest

from scripts.events import redis_state

pytestmark = pytest.mark.unit


def test_history_trim_is_bounded_and_preserves_current_documents() -> None:
    import fakeredis

    from scripts.events import state_history

    client = fakeredis.FakeRedis(decode_responses=True)
    states = redis_state.RedisStateStore(client, "probe", prefix="fixture")
    revision = states.create("doc", b"current")
    history = "fixture:state:probe:history:doc"
    current = client.hget("fixture:state:probe:current", "doc")
    client.delete(history)
    client.rpush(history, "oldest", "older", "old", "prior", current)
    client.rpush("outside:state:probe:history:doc", "untouched", "untouched", "untouched")
    cursor, removed = state_history.trim_page(client, "fixture", limit=2)
    assert (cursor, removed) == (0, 2)
    assert client.lrange(history, 0, -1) == ["old", "prior", current]
    assert client.hget("fixture:state:probe:current", "doc") == current
    assert states.get("doc").revision == revision
    cursor, removed = state_history.trim_page(client, "fixture", cursor=cursor, limit=2)
    assert (cursor, removed) == (0, 1)
    assert client.lrange(history, 0, -1) == ["prior", current]
    assert client.llen("outside:state:probe:history:doc") == 3
    assert state_history.trim_page(client, "fixture") == (0, 0)
