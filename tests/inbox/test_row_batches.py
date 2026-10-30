import pytest

from scripts.inbox.store import InboxStore


@pytest.fixture
def store():
    import fakeredis

    return InboxStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))


def closing_pipelines(store, monkeypatch, closes):
    """Pipelines whose first ``closes`` executions fail the way a server side close does."""
    from redis.exceptions import ConnectionError as RedisConnectionError

    real = store.redis.pipeline
    sizes = []

    def pipeline(*args, **kwargs):
        pipe = real(*args, **kwargs)
        execute = pipe.execute

        def counted():
            sizes.append(len(pipe.command_stack))
            if len(sizes) <= closes:
                raise RedisConnectionError("Connection closed by server.")
            return execute()

        pipe.execute = counted
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", pipeline)
    return sizes


def test_rows_are_read_in_pipelines_of_five_hundred_in_order(store, monkeypatch):
    item = store.send("alice", "bob", "the last row")
    sizes = closing_pipelines(store, monkeypatch, closes=0)
    rows = store._rows([f"missing-{n}" for n in range(1000)] + [item.id])
    assert sizes == [500, 500, 1]
    assert len(rows) == 1001
    assert rows[:1000] == [{}] * 1000
    assert rows[1000]["text"] == "the last row"


def test_a_listing_reads_again_after_the_server_closes_the_connection(store, monkeypatch):
    item = store.send("alice", "bob", "still here")
    sizes = closing_pipelines(store, monkeypatch, closes=1)
    assert [(listed.id, listed.text) for listed in store.inbox("bob")] == [(item.id, "still here")]
    assert sizes == [1, 1]


def test_a_second_close_on_the_same_batch_is_raised(store, monkeypatch):
    from redis.exceptions import ConnectionError as RedisConnectionError

    store.send("alice", "bob", "hi")
    sizes = closing_pipelines(store, monkeypatch, closes=2)
    with pytest.raises(RedisConnectionError, match="Connection closed by server"):
        store.inbox("bob")
    assert sizes == [1, 1]


def test_an_empty_read_opens_no_pipeline(store, monkeypatch):
    sizes = closing_pipelines(store, monkeypatch, closes=0)
    assert store._rows([]) == []
    assert sizes == []
