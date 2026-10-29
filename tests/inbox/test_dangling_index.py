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
    """A sent item whose record is gone while every index of its address still lists it."""
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
    ("listing", "found"),
    [
        (lambda s, a: s.inbox(a), True),
        (lambda s, a: s.open_items(a), True),
        (lambda s, a: s.pending_items(a), True),
        (lambda s, a: s.pending(), True),
        (lambda s, a: s.unsettled(a, 10**15), False),
    ],
)
def test_a_listing_skips_logs_and_drops_an_id_whose_record_is_gone(store, capsys, listing, found):
    gone = dangle(store, "bob")
    kept = store.send("carol", "bob", "here")
    assert [item.id for item in listing(store, "bob")] == ([kept.id] if found else [])
    assert_dropped(store, "bob", [kept.id], capsys, gone)
    assert [item.id for item in listing(store, "bob")] == ([kept.id] if found else [])
    assert capsys.readouterr().err == ""


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
