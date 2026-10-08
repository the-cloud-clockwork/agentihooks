import pytest

import hooks.context.inbox_delivery as delivery
from scripts.inbox.store import InboxError, InboxStore


class CountingRedis:
    def __init__(self, redis):
        self._redis = redis
        self.calls = []

    def __getattr__(self, name):
        attr = getattr(self._redis, name)
        if not callable(attr) or name == "pipeline":
            return attr

        def counted(*args, **kwargs):
            self.calls.append(name)
            return attr(*args, **kwargs)

        return counted


@pytest.fixture
def redis():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


@pytest.fixture
def store(redis):
    return InboxStore(redis)


def pending_ids(store, address):
    return store.redis.zrange(store.key("pending", address), 0, -1)


def test_send_adds_the_item_to_the_addressee_pending_set(store):
    item = store.send("alice", "bob", "hi")
    assert pending_ids(store, "bob") == [item.id]
    assert pending_ids(store, "alice") == []


@pytest.mark.parametrize(
    "leave",
    [
        lambda s, i: s.deliver(i, "bob"),
        lambda s, i: s.read(i, "bob"),
        lambda s, i: s.close(i, "bob", "done", "handled the request"),
        lambda s, i: s.close(i, "alice", "cancel"),
        lambda s, i: s.close(i, "bob", "blocked", "a review"),
        lambda s, i: s.close(i, "bob", "handoff", "carol"),
    ],
    ids=["delivered", "read", "done", "cancelled", "blocked", "handed_off"],
)
def test_each_leaving_transition_removes_the_item_from_the_pending_set(store, leave):
    item = store.send("alice", "bob", "hi")
    kept = store.send("alice", "bob", "still pending")
    leave(store, item.id)
    assert pending_ids(store, "bob") == [kept.id]
    assert [i.id for i in store.pending_items("bob")] == [kept.id]


def test_delivery_reads_only_the_pending_set_with_a_thousand_closed_items(redis, monkeypatch):
    seed = InboxStore(redis)
    for n in range(1000):
        seed.close(seed.send("alice", "bob", f"old {n}").id, "bob", "done", "handled the request")
    item = seed.send("alice", "bob", "new work")
    counting = CountingRedis(redis)
    store = InboxStore(counting)
    store.pending_items("bob")
    monkeypatch.setattr(delivery, "connect", lambda environ=None: store)
    counting.calls.clear()

    out = delivery.pending_context("s1", {"AGENTIHOOKS_AGENT_NAME": "bob"})

    assert item.id in out and "new work" in out
    assert len(counting.calls) <= 7, counting.calls
    assert seed.get(item.id).state == "delivered"
    assert len(store.inbox("bob")) == 1001


def test_the_wake_pass_listing_reads_only_pending_sets(redis):
    seed = InboxStore(redis)
    for n in range(1000):
        seed.close(seed.send("alice", "bob", f"old {n}").id, "bob", "done", "handled the request")
    item = seed.send("alice", "carol", "new work")
    counting = CountingRedis(redis)
    store = InboxStore(counting)
    store.pending()
    counting.calls.clear()

    assert [i.id for i in store.pending()] == [item.id]
    assert counting.calls.count("hgetall") == 1, counting.calls


def test_back_fill_rebuilds_the_pending_set_from_existing_items(store, monkeypatch):
    from scripts.inbox import store as inbox_store

    monkeypatch.setattr(inbox_store, "now_ms", lambda: 1000)
    first = store.send("alice", "bob", "one")
    second = store.send("carol", "bob", "two")
    store.close(store.send("alice", "bob", "old").id, "bob", "done", "handled the request")
    store.deliver(store.send("alice", "bob", "seen").id, "bob")
    store.redis.delete(store.key("pending", "bob"), store.key("indexed"))

    assert [i.id for i in store.pending_items("bob")] == [first.id, second.id]
    assert sorted(pending_ids(store, "bob")) == sorted([first.id, second.id])
    assert store.redis.sismember(store.key("indexed"), "bob")


def test_back_fill_runs_once_and_keeps_items_sent_before_it(store, monkeypatch):
    from scripts.inbox import store as inbox_store

    monkeypatch.setattr(inbox_store, "now_ms", lambda: 1000)
    ids = iter([inbox_store.uuid.UUID(int=2 << 80), inbox_store.uuid.UUID(int=1 << 80)])
    monkeypatch.setattr(inbox_store.uuid, "uuid4", lambda: next(ids))
    old = store.send("alice", "bob", "before the change")
    store.redis.delete(store.key("pending", "bob"), store.key("indexed"))
    new = store.send("alice", "bob", "after the change")

    for _ in range(5):
        assert [i.id for i in store.pending_items("bob")] == [old.id, new.id]
    store.deliver(old.id, "bob")
    assert [i.id for i in store.pending_items("bob")] == [new.id]


@pytest.mark.parametrize("read", ["inbox", "pending_items", "pending_mail", "pending"])
def test_reads_keep_send_order_at_equal_times_across_connections(store, monkeypatch, read):
    from scripts.inbox import store as inbox_store

    monkeypatch.setattr(inbox_store, "now_ms", lambda: 1000)
    ids = iter([inbox_store.uuid.UUID(int=2 << 80), inbox_store.uuid.UUID(int=1 << 80)])
    monkeypatch.setattr(inbox_store.uuid, "uuid4", lambda: next(ids))
    store.pending_items("bob")
    old = store.send("alice", "bob", "first")
    new = store.send("alice", "bob", "second")
    for _ in range(5):
        fresh = inbox_store.InboxStore(store.redis)
        items = fresh.pending() if read == "pending" else getattr(fresh, read)("bob")
        assert [item.id for item in items] == [old.id, new.id]


@pytest.mark.parametrize("read", ["pending", "pending_mail"])
def test_pending_reads_merge_addresses_oldest_first(store, monkeypatch, read):
    from scripts.inbox import store as inbox_store

    times = iter([1000, 2000, 3000])
    monkeypatch.setattr(inbox_store, "now_ms", lambda: next(times))
    store.seats.occupy("eng-1@demo", "bob", 500)
    old = store.send("alice", "eng-1@demo", "first")
    middle = store.send("alice", "bob", "second")
    new = store.send("alice", "bob", "third")
    for _ in range(5):
        items = store.pending() if read == "pending" else store.pending_mail("bob")
        assert [item.id for item in items] == [old.id, middle.id, new.id]


def test_legacy_pending_items_without_sequence_stay_oldest_first(store, monkeypatch):
    from scripts.inbox import store as inbox_store

    times = iter([1000, 2000])
    monkeypatch.setattr(inbox_store, "now_ms", lambda: next(times))
    old = store.send("alice", "bob", "legacy")
    store.redis.hdel(store.key("item", old.id), "sequence")
    new = store.send("alice", "bob", "new")
    store.redis.delete(store.key("pending", "bob"), store.key("indexed"))
    for _ in range(5):
        assert [item.id for item in store.pending_items("bob")] == [old.id, new.id]


def test_a_failed_transition_leaves_the_item_and_the_pending_set_consistent(store, monkeypatch):
    import redis as redis_lib

    item = store.send("alice", "bob", "hi")
    real = store.redis.pipeline

    def failing(*args, **kwargs):
        pipe = real(*args, **kwargs)

        def boom(*a, **k):
            raise redis_lib.exceptions.ConnectionError("lost the server")

        pipe.execute = boom
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", failing)
    with pytest.raises(redis_lib.exceptions.ConnectionError):
        store.close(item.id, "bob", "done", "handled the request")
    monkeypatch.undo()

    assert store.get(item.id).state == "pending"
    assert pending_ids(store, "bob") == [item.id]


def test_a_refused_transition_leaves_the_pending_set_alone(store):
    item = store.send("alice", "bob", "hi")
    with pytest.raises(InboxError):
        store.read(item.id, "mallory")
    assert pending_ids(store, "bob") == [item.id]


def test_a_send_racing_the_back_fill_is_in_the_result_and_the_set(store, monkeypatch):
    import scripts.inbox.store as module

    monkeypatch.setattr(module, "now_ms", lambda: 1000)
    old = store.send("alice", "bob", "before the change")
    store.redis.delete(store.key("pending", "bob"), store.key("indexed"))
    real, raced = module._item, []

    def racing(raw, item_id):
        if not raced:
            raced.append(store.send("carol", "bob", "raced"))
        return real(raw, item_id)

    monkeypatch.setattr(module, "_item", racing)
    assert [i.id for i in store.pending_items("bob")] == [old.id, raced[0].id]
    assert sorted(pending_ids(store, "bob")) == sorted([old.id, raced[0].id])
    assert store.redis.sismember(store.key("indexed"), "bob")


def waiting(store):
    return store.redis.smembers(store.key("waiting"))


def test_a_send_adds_its_address_to_the_waiting_set(store):
    store.send("alice", "bob", "hi")
    assert waiting(store) == {"bob"}


def test_emptying_a_pending_list_removes_its_address_from_the_waiting_set(store):
    first = store.send("alice", "bob", "one")
    second = store.send("alice", "bob", "two")
    store.deliver(first.id, "bob")
    assert waiting(store) == {"bob"}
    store.close(second.id, "bob", "done", "handled the request")
    assert waiting(store) == set()


def test_the_wake_pass_reads_only_the_waiting_set_with_many_idle_addresses(redis):
    seed = InboxStore(redis)
    for n in range(300):
        seed.close(seed.send("alice", f"idle-{n}", "old").id, f"idle-{n}", "done", "handled the request")
    item = seed.send("alice", "carol", "new work")
    counting = CountingRedis(redis)
    store = InboxStore(counting)
    store.pending()
    counting.calls.clear()

    assert [i.id for i in store.pending()] == [item.id]
    assert "scan_iter" not in counting.calls and "scan" not in counting.calls, counting.calls
    assert len(counting.calls) <= 7, counting.calls


def test_back_fill_rebuilds_the_waiting_set_once(store):
    first = store.send("alice", "bob", "one")
    store.close(store.send("alice", "carol", "old").id, "carol", "done", "handled the request")
    second = store.send("alice", "dave", "two")
    store.redis.delete(store.key("waiting"), store.key("waiting", "built"), store.key("indexed"))
    store.redis.delete(store.key("pending", "bob"), store.key("pending", "dave"))

    assert sorted(i.id for i in store.pending()) == sorted([first.id, second.id])
    assert waiting(store) == {"bob", "dave"}
    store.redis.srem(store.key("waiting"), "dave")
    assert [i.id for i in store.pending()] == [first.id]
