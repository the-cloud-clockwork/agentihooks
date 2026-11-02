from collections.abc import Callable
from typing import Any

import pytest

from scripts.inbox.store import ROW_BATCH, InboxStore, Item, _fields

ADDRESS = "eng-6@crew"
STATES = ("pending", "delivered", "done", "read")


def seeded(count: int) -> InboxStore:
    import fakeredis

    store = InboxStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    with store.redis.pipeline() as pipe:
        for index in range(count):
            item_id = f"message-{index:05}"
            item = Item(item_id, "alice", ADDRESS, item_id, STATES[index % 4], index, index, sequence=index)
            pipe.hset(store.key("item", item_id), mapping=_fields(item))
            pipe.zadd(store.key("address", ADDRESS), {item_id: index})
        pipe.execute()
    return store


def bounded(store: InboxStore, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    from redis.exceptions import ConnectionError as RedisConnectionError

    real = store.redis.pipeline
    reads: list[tuple[str, int]] = []

    def check(kind: str, size: int) -> None:
        reads.append((kind, size))
        if size > ROW_BATCH:
            raise RedisConnectionError(f"Connection reset by peer: {kind} of {size}")

    def pipeline(*args: Any, **kwargs: Any) -> Any:
        pipe = real(*args, **kwargs)
        watch, zrange, execute = pipe.watch, pipe.zrange, pipe.execute

        def watched(*names: str) -> Any:
            check("watch", len(names))
            return watch(*names)

        def ranged(key: str, start: int, end: int, *rest: Any, **options: Any) -> Any:
            check("zrange", ROW_BATCH + 1 if end < 0 else end - start + 1)
            return zrange(key, start, end, *rest, **options)

        def executed(*rest: Any, **options: Any) -> Any:
            check("pipeline", len(pipe.command_stack))
            return execute(*rest, **options)

        pipe.watch, pipe.zrange, pipe.execute = watched, ranged, executed
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", pipeline)
    return reads


def expected(count: int, keep: Callable[[str], bool]) -> list[str]:
    return [f"message-{index:05}" for index in range(count) if keep(STATES[index % 4])]


@pytest.mark.parametrize("count", [0, 1, 500, 501, 2001])
def test_open_index_rebuild_reads_a_large_mailbox_in_bounded_batches(
    count: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = seeded(count)
    reads = bounded(store, monkeypatch)
    items = store.open_items(ADDRESS)
    assert [item.id for item in items] == expected(count, lambda state: state != "done")
    assert store.redis.zcard(store.key("open", ADDRESS)) == len(items)
    assert store.redis.get(store.key("open-size", ADDRESS)) == str(count)
    assert ("zrange", ROW_BATCH) in reads
    assert max(size for _, size in reads) <= ROW_BATCH


@pytest.mark.parametrize("count", [0, 1, 500, 501, 2001])
def test_pending_back_fill_reads_a_large_mailbox_in_bounded_batches(
    count: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = seeded(count)
    reads = bounded(store, monkeypatch)
    items = store.pending_items(ADDRESS)
    assert [item.id for item in items] == expected(count, lambda state: state == "pending")
    assert store.redis.sismember(store.key("indexed"), ADDRESS)
    assert store.redis.zcard(store.key("pending", ADDRESS)) == len(items)
    assert ("zrange", ROW_BATCH) in reads
    assert max(size for _, size in reads) <= ROW_BATCH


def test_an_item_changed_during_a_batched_rebuild_aborts_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    store = seeded(ROW_BATCH + 1)
    batch = store._batch
    calls: list[int] = []

    def racing(item_ids: list[str]) -> list[dict[str, str]]:
        calls.append(len(item_ids))
        if len(calls) == 2:
            store.redis.hset(store.key("item", "message-00000"), "state", "done")
        return batch(item_ids)

    monkeypatch.setattr(store, "_batch", racing)
    items = store.open_items(ADDRESS)
    assert calls[:3] == [ROW_BATCH, 1, ROW_BATCH]
    assert "message-00000" not in [item.id for item in items]
    assert store.redis.zscore(store.key("open", ADDRESS), "message-00000") is None
