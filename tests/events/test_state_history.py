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


@pytest.mark.parametrize(("cursor", "limit"), [(0, 0), (0, 129), (-1, 16)])
def test_trim_refuses_invalid_bounds(cursor: int, limit: int) -> None:
    from scripts.events import state_history

    with pytest.raises(ValueError) as error:
        state_history.trim_page(None, "fixture", cursor=cursor, limit=limit)
    assert str(error.value) == "limit must be between 1 and 128 and cursor must be nonnegative"


def test_trim_preserves_a_concurrent_state_write(monkeypatch: pytest.MonkeyPatch) -> None:
    import fakeredis

    from scripts.events import state_history

    client = fakeredis.FakeRedis()
    states = redis_state.RedisStateStore(client, "probe", prefix="fixture")
    revision = states.create("doc", b"prior")
    history = "fixture:state:probe:history:doc"
    client.lpush(history, b"old", b"older")
    evaluate = client.eval

    def concurrent_write(script: str, numkeys: int, key: bytes, keep: int) -> int:
        states.update("doc", b"new", revision)
        return evaluate(script, numkeys, key, keep)

    monkeypatch.setattr(client, "eval", concurrent_write)
    assert state_history.trim_page(client, "fixture") == (0, 0)
    assert [(row.revision, row.value) for row in states.watch("doc")] == [(1, b"prior"), (2, b"new")]


def test_trim_advances_the_scan_and_honors_the_removal_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    import fakeredis

    from scripts.events import state_history

    client = fakeredis.FakeRedis()
    first = "fixture:state:probe:history:first"
    second = "fixture:state:probe:history:second"
    client.rpush(first, b"old", b"prior", b"current")
    client.rpush(second, b"old", b"prior", b"current")

    def scan(cursor: int, match: str, count: int) -> tuple[int, list[str]]:
        assert cursor == 7
        assert match == "fixture:state:*:history:*"
        assert count == 16
        return 23, [first, second]

    monkeypatch.setattr(client, "scan", scan)
    assert state_history.trim_page(client, "fixture", cursor=7, limit=1) == (7, 1)
    assert client.lrange(first, 0, -1) == [b"prior", b"current"]
    assert client.lrange(second, 0, -1) == [b"old", b"prior", b"current"]
    assert state_history.trim_page(client, "fixture", cursor=7, limit=128) == (23, 1)
    assert client.lrange(second, 0, -1) == [b"prior", b"current"]


@pytest.mark.parametrize("argv", [[], ["--cursor", "7", "--limit", "2"]])
def test_command_prints_its_resume_cursor(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.events import state_history
    from scripts.swarm import store

    client = object()
    monkeypatch.setattr(store, "redis_client", lambda: client)
    monkeypatch.setattr(state_history, "_prefix", lambda: "fixture")

    def trim(redis: object, prefix: str, cursor: int, limit: int) -> tuple[int, int]:
        assert redis is client
        assert prefix == "fixture"
        assert (cursor, limit) == ((7, 2) if argv else (0, 16))
        return 31, 2

    monkeypatch.setattr(state_history, "trim_page", trim)
    assert state_history.main(argv) == 0
    assert capsys.readouterr().out == '{"cursor": 31, "removed": 2}\n'


def test_command_refuses_an_unavailable_store(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.events import state_history
    from scripts.swarm import store

    def unavailable() -> None:
        raise ConnectionError("Redis is unavailable")

    monkeypatch.setattr(store, "redis_client", unavailable)
    with pytest.raises(ConnectionError) as error:
        state_history.main([])
    assert str(error.value) == "Redis is unavailable"
