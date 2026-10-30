from unittest.mock import patch

import fakeredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from scripts.inbox.store import InboxStore, Item, _fields


@pytest.mark.parametrize("count", [0, 1, 500, 501, 2001])
def test_seat_mailbox_listing_bounds_index_reads(count: int) -> None:
    store = InboxStore(fakeredis.FakeRedis(decode_responses=True))
    address = "eng-6@crew"
    expected = [f"message-{index:05}" for index in range(count)]
    with store.redis.pipeline() as pipe:
        for index, item_id in enumerate(expected):
            item = Item(item_id, "alice", address, item_id, "pending", index, index, sequence=index)
            pipe.hset(store.key("item", item_id), mapping=_fields(item))
            pipe.zadd(store.key("address", address), {item_id: index})
        pipe.execute()
    read = store.redis.zrange
    pages: list[tuple[int, int]] = []

    def bounded_read(key: str, start: int, end: int) -> list[str]:
        if end < start or end - start >= 500:
            raise RedisConnectionError("Connection reset by peer: oversized mailbox read")
        pages.append((start, end))
        return read(key, start, end)

    with patch.object(store.redis, "zrange", side_effect=bounded_read):
        items = store.inbox(address)

    assert [item.id for item in items] == expected
    assert pages == [(start, start + 499) for start in range(0, count + 1, 500)]
