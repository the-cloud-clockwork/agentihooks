from types import SimpleNamespace

import pytest

from scripts.inbox.seats import seat_address
from scripts.inbox.store import InboxError, InboxStore
from scripts.swarm import tick


@pytest.fixture
def redis():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


@pytest.fixture
def store(redis):
    return InboxStore(redis)


def dangle(store, address):
    """A sent item whose record is gone while every current index of its address still lists it."""
    store.open_items(address)
    store.pending_items(address)
    item = store.send("alice", address, "lost")
    store.redis.delete(store.key("item", item.id))
    return item.id


def indexed(store, address):
    return {kind: store.redis.zrange(store.key(kind, address), 0, -1) for kind in ("address", "open", "pending")}


def assert_dropped(store, address, kept, capsys, gone):
    assert indexed(store, address) == {"address": kept, "open": kept, "pending": kept}
    assert capsys.readouterr().err.splitlines() == [
        f"agentihooks inbox: {store.key('address', address)} lists message {gone} whose record is gone; dropped it"
    ]


@pytest.mark.parametrize(
    "listing",
    [
        lambda s, a: s.inbox(a),
        lambda s, a: s.open_items(a),
        lambda s, a: s.pending_items(a),
        lambda s, a: s.pending(),
    ],
)
def test_a_listing_skips_logs_and_drops_an_id_whose_record_is_gone(store, capsys, listing):
    gone = dangle(store, "bob")
    kept = store.send("carol", "bob", "here")
    assert [item.id for item in listing(store, "bob")] == [kept.id]
    assert_dropped(store, "bob", [kept.id], capsys, gone)
    assert [item.id for item in listing(store, "bob")] == [kept.id]
    assert capsys.readouterr().err == ""


def test_the_unsettled_listing_skips_logs_and_drops_an_id_whose_record_is_gone(store, capsys):
    gone = dangle(store, "bob")
    kept = store.send("carol", "bob", "here")
    store.read(kept.id, "bob")
    assert [item.id for item in store.unsettled("bob", 10**15)] == [kept.id]
    assert store.redis.zrange(store.key("address", "bob"), 0, -1) == [kept.id]
    assert store.redis.zrange(store.key("open", "bob"), 0, -1) == [kept.id]
    assert capsys.readouterr().err.splitlines() == [
        f"agentihooks inbox: {store.key('address', 'bob')} lists message {gone} whose record is gone; dropped it"
    ]


def test_a_drop_keeps_the_open_index_current(store):
    kept = store.send("carol", "bob", "here")
    assert [item.id for item in store.open_items("bob")] == [kept.id]
    dangle(store, "bob")
    dangle(store, "bob")
    assert [item.id for item in store.inbox("bob")] == [kept.id]
    assert store.redis.get(store.key("open-size", "bob")) == "1"
    assert store.redis.sismember(store.key("open-indexed"), "bob")


def test_an_open_index_rebuild_skips_and_drops_an_id_whose_record_is_gone(store, capsys):
    gone = dangle(store, "bob")
    kept = store.send("carol", "bob", "here")
    store.redis.srem(store.key("open-indexed"), "bob")
    store.redis.srem(store.key("indexed"), "bob")
    store.redis.delete(store.key("pending", "bob"))
    assert [item.id for item in store.open_items("bob")] == [kept.id]
    assert store.redis.zrange(store.key("address", "bob"), 0, -1) == [kept.id]
    assert store.redis.get(store.key("open-size", "bob")) == "1"
    assert [item.id for item in store.pending_items("bob")] == [kept.id]
    assert capsys.readouterr().err.splitlines() == [
        f"agentihooks inbox: {store.key('address', 'bob')} lists message {gone} whose record is gone; dropped it"
    ]


def test_a_single_item_read_still_raises_for_a_missing_record(store):
    gone = dangle(store, "bob")
    with pytest.raises(InboxError, match=f"^no message {gone}$"):
        store.get(gone)
    with pytest.raises(InboxError, match=f"^no message {gone}$"):
        store.read(gone, "bob")


def test_the_tick_master_wake_pass_completes_past_a_dangling_id(store, capsys):
    seat = seat_address("sw", "master")
    gone = dangle(store, seat)
    kept = store.send("operator", seat, "wake up")
    swarm = SimpleNamespace(redis=store.redis)
    assert tick._woken("sw", SimpleNamespace(template="plain"), swarm, None) is True
    assert_dropped(store, seat, [kept.id], capsys, gone)


def test_the_doctor_inbox_read_skips_and_drops_an_id_whose_record_is_gone(store, capsys):
    from scripts.doctor import read

    seat = seat_address("sw", "master")
    gone = dangle(store, seat)
    kept = store.send("operator", seat, "here")
    assert [row["id"] for row in read.inbox_items(store, "sw")] == [kept.id]
    assert_dropped(store, seat, [kept.id], capsys, gone)


def test_the_pool_routes_skip_and_drop_an_id_whose_record_is_gone(store, capsys, monkeypatch):
    from scripts.inbox import pool

    kept = store.send("operator", "bob", "here")
    index = store.pool.key("sw")
    store.redis.zadd(index, {"gone1": 1, kept.id: 2})
    monkeypatch.delenv(pool.intent.FLAG, raising=False)
    monkeypatch.setattr(pool.intent, "routes", lambda _pool, slug, items, live: [item.id for item in items])
    assert store.pool._routes("sw", []) == [kept.id]
    assert store.redis.zrange(index, 0, -1) == [kept.id]
    assert capsys.readouterr().err.splitlines() == [
        f"agentihooks inbox: {index} lists message gone1 whose record is gone; dropped it"
    ]


def test_a_requeue_drops_and_logs_a_delivered_id_whose_record_is_gone(store, capsys):
    store.redis.zadd(store.key("delivered"), {"gone1": 1})
    assert store.requeue("gone1", "inbox", "redelivered") is None
    assert store.redis.zrange(store.key("delivered"), 0, -1) == []
    assert capsys.readouterr().err.splitlines() == [
        f"agentihooks inbox: {store.key('delivered')} lists message gone1 whose record is gone; dropped it"
    ]


def test_a_shown_confirm_drops_and_logs_a_delivered_id_whose_record_is_gone(redis, capsys):
    import json

    store = InboxStore(redis, session_id="s1")
    store.redis.zadd(store.key("delivered"), {"gone1": 1})
    store.redis.rpush(store.key("history", "gone1"), json.dumps({"state": "delivered", "by": "bob", "at": 1}))
    kept = store.deliver(store.send("alice", "bob", "here").id, "bob")
    assert [item.id for item in store.confirm_shown("bob", kept.updated_at + 1)] == [kept.id]
    assert store.redis.zrange(store.key("delivered"), 0, -1) == []
    assert capsys.readouterr().err.splitlines() == [
        f"agentihooks inbox: {store.key('delivered')} lists message gone1 whose record is gone; dropped it"
    ]
