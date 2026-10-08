import pytest

from scripts.inbox.store import InboxError, InboxStore
from scripts.swarm.keyspace import ROOT as KEY_ROOT

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def server():
    import fakeredis

    return fakeredis.FakeServer()


def fresh(server):
    import fakeredis

    return InboxStore(fakeredis.FakeRedis(server=server, decode_responses=True))


@pytest.fixture
def store(server):
    return fresh(server)


def test_send_then_inbox_shows_a_pending_item_from_the_sender(store):
    item = store.send("alice", "bob", "review my branch")
    [listed] = store.inbox("bob")
    assert (listed.id, listed.sender, listed.address, listed.text, listed.state) == (
        item.id,
        "alice",
        "bob",
        "review my branch",
        "pending",
    )
    assert store.inbox("alice") == []


def test_read_marks_the_item_read(store):
    item = store.send("alice", "bob", "hi")
    assert store.read(item.id, "bob").state == "read"
    assert store.get(item.id).state == "read"


def test_only_the_addressee_reads(store):
    item = store.send("alice", "bob", "hi")
    with pytest.raises(InboxError):
        store.read(item.id, "mallory")
    assert store.get(item.id).state == "pending"


@pytest.mark.parametrize("kind", ["", "unknown"])
def test_closing_without_a_reason_is_refused(store, kind):
    item = store.send("alice", "bob", "hi")
    with pytest.raises(InboxError):
        store.close(item.id, "bob", kind)
    assert store.get(item.id).state == "pending"


@pytest.mark.parametrize("kind", ["handoff", "blocked"])
def test_handoff_and_blocked_need_their_detail(store, kind):
    item = store.send("alice", "bob", "hi")
    with pytest.raises(InboxError):
        store.close(item.id, "bob", kind, "")


@pytest.mark.parametrize(
    "kind, detail, state, reason",
    [
        ("done", "handled the request", "done", "done: handled the request"),
        ("handoff", "carol", "handed_off", "handed off to carol"),
        ("blocked", "the release", "blocked", "blocked on the release"),
        ("cancel", "", "cancelled", "cancelled"),
        ("cancel", "duplicate", "cancelled", "cancelled: duplicate"),
    ],
)
def test_close_records_where_the_work_went(store, kind, detail, state, reason):
    item = store.send("alice", "bob", "hi")
    closed = store.close(item.id, "bob", kind, detail)
    assert (closed.state, closed.reason) == (state, reason)


def test_the_sender_may_cancel_but_a_stranger_may_not_close(store):
    item = store.send("alice", "bob", "hi")
    with pytest.raises(InboxError):
        store.close(item.id, "mallory", "done", "handled the request")
    assert store.close(item.id, "alice", "cancel").state == "cancelled"


def test_the_master_closes_items_left_for_a_seat_or_agent_of_its_swarm(store):
    store.seats.occupy("master@sw", "sw-master-1", 1)
    by_name = store.send("sw-eng-1", "sw-eng-2", "contract confirmed")
    by_seat = store.send("sw-eng-1", "eng-2@sw", "contract confirmed")
    assert store.close(by_name.id, "sw-master-1", "cancel", "engineer two exited").state == "cancelled"
    assert store.close(by_seat.id, "sw-master-1", "cancel", "engineer two exited").state == "cancelled"


def test_another_swarms_master_may_not_close_an_item_of_this_swarm(store):
    store.seats.occupy("master@other", "other-master-1", 1)
    item = store.send("sw-eng-1", "sw-eng-2", "contract confirmed")
    with pytest.raises(InboxError, match="belongs to sw-eng-2"):
        store.close(item.id, "other-master-1", "cancel")


def test_a_closed_item_stays_closed(store):
    item = store.send("alice", "bob", "hi")
    store.close(item.id, "bob", "done", "handled the request")
    with pytest.raises(InboxError):
        store.close(item.id, "bob", "cancel")
    with pytest.raises(InboxError):
        store.read(item.id, "bob")


def test_history_keeps_every_transition_in_order(store):
    item = store.send("alice", "bob", "hi")
    store.read(item.id, "bob")
    store.close(item.id, "bob", "handoff", "carol")
    history = store.history(item.id)
    assert [(h["state"], h["by"], h["reason"]) for h in history] == [
        ("pending", "alice", ""),
        ("read", "bob", ""),
        ("handed_off", "bob", "handed off to carol"),
    ]
    assert [h["at"] for h in history] == sorted(h["at"] for h in history)
    assert store.get(item.id).updated_at == history[-1]["at"]


def test_items_survive_a_fresh_store_connection(server, store):
    item = store.send("alice", "bob", "hi")
    store.read(item.id, "bob")
    again = fresh(server)
    assert again.inbox("bob") == [again.get(item.id)]
    assert again.get(item.id).state == "read"
    assert [h["state"] for h in again.history(item.id)] == ["pending", "read"]


def test_items_carry_no_expiry(store):
    item = store.send("alice", "bob", "hi")
    assert all(store.redis.ttl(key) == -1 for key in store.redis.keys(f"{KEY_ROOT}:inbox:*"))
    assert store.get(item.id)


def test_an_unknown_id_is_refused(store):
    with pytest.raises(InboxError):
        store.get("nope")


def test_deliver_moves_a_pending_item_once_and_records_it(store):
    item = store.send("alice", "bob", "hi")
    delivered = store.deliver(item.id, "bob")
    assert delivered.state == "delivered"
    assert store.get(item.id).state == "delivered"
    assert store.deliver(item.id, "bob") is None
    assert [e["state"] for e in store.history(item.id)] == ["pending", "delivered"]


def test_deliver_skips_an_item_that_is_no_longer_pending(store):
    item = store.send("alice", "bob", "hi")
    store.read(item.id, "bob")
    assert store.deliver(item.id, "bob") is None
    assert store.get(item.id).state == "read"


def test_deliver_never_moves_another_address_item(store):
    item = store.send("alice", "bob", "hi")
    assert store.deliver(item.id, "carol") is None
    assert store.get(item.id).state == "pending"


def test_two_racing_deliveries_have_one_winner(server, monkeypatch):
    import scripts.inbox.store as inbox_store

    first, second = fresh(server), fresh(server)
    item = first.send("alice", "bob", "hi")
    real_now, raced = inbox_store.now_ms, []

    def racing_now():
        if not raced:
            raced.append(None)
            raced[0] = second.deliver(item.id, "bob")
        return real_now()

    monkeypatch.setattr(inbox_store, "now_ms", racing_now)
    assert first.deliver(item.id, "bob") is None
    assert raced[0].state == "delivered"
    assert [e["state"] for e in first.history(item.id)] == ["pending", "delivered"]


def test_reply_goes_to_the_sender_and_closes_the_original_done(store):
    item = store.send("operator", "master", "how far along are we")
    answer = store.reply(item.id, "master", "two tasks left")
    assert (answer.sender, answer.address, answer.text, answer.state) == (
        "master",
        "operator",
        "two tasks left",
        "pending",
    )
    closed = store.get(item.id)
    assert closed.state == "done" and answer.id in closed.reason


def test_only_the_addressee_can_reply(store):
    item = store.send("alice", "bob", "hi")
    with pytest.raises(InboxError):
        store.reply(item.id, "mallory", "me too")
    assert store.inbox("alice") == []


def test_open_index_keeps_delivered_and_read_until_closed(store, monkeypatch):
    first = store.send("sender", "receiver", "first")
    second = store.send("sender", "receiver", "second")
    third = store.send("sender", "receiver", "third")
    store.deliver(second.id, "receiver")
    store.read(third.id, "receiver")
    assert [i.id for i in store.open_items("receiver")] == [first.id, second.id, third.id]
    store.close(first.id, "receiver", "done", "finished")
    seen = []
    original = store.get

    def get(item_id):
        seen.append(item_id)
        return original(item_id)

    monkeypatch.setattr(store, "get", get)
    index = store._index_open

    def rebuild(address):
        assert address != "receiver", "unchanged mailbox was rebuilt"
        return index(address)

    monkeypatch.setattr(store, "_index_open", rebuild)
    assert [i.id for i in store.open_items("receiver")] == [second.id, third.id]
    assert sorted(seen) == sorted([second.id, third.id])
    store.redirect(second.id, "swarm", "seat", "receiver exited", "receiver")
    assert [i.id for i in store.open_items("receiver")] == [third.id]
    assert [i.id for i in store.open_items("seat")] == [second.id]
    store.close(third.id, "receiver", "blocked", "missing decision")
    store.close(second.id, "seat", "cancel", "obsolete")
    assert store.open_items("receiver") == []
    assert store.open_items("seat") == []


def test_open_index_backfills_old_mail_and_preserves_closed_history(store):
    first = store.send("sender", "receiver", "pending")
    second = store.send("sender", "receiver", "delivered")
    third = store.send("sender", "receiver", "read")
    fourth = store.send("sender", "receiver", "closed")
    store.deliver(second.id, "receiver")
    store.read(third.id, "receiver")
    store.close(fourth.id, "receiver", "done", "finished")
    store.redis.zadd(store.key("open", "receiver"), {fourth.id: fourth.created_at})
    store.redis.srem(store.key("open-indexed"), "receiver")
    assert [i.id for i in store.open_items("receiver")] == [first.id, second.id, third.id]
    assert len(store.inbox("receiver")) == 4
    assert store.redis.sismember(store.key("open-indexed"), "receiver")


@pytest.mark.parametrize("conflicts", [1, 3])
def test_open_index_retries_concurrent_mail_changes(store, monkeypatch, conflicts):
    from redis.exceptions import WatchError

    item = store.send("sender", "receiver", "open work")
    closed = store.send("sender", "receiver", "closed work")
    store.close(closed.id, "receiver", "done", "finished")
    index = store._index_open
    calls = []

    def racing(address):
        calls.append(address)
        if len(calls) <= conflicts:
            raise WatchError()
        return index(address)

    monkeypatch.setattr(store, "_index_open", racing)
    assert [i.id for i in store.open_items("receiver")] == [item.id]
    assert calls == ["receiver"] * min(conflicts + 1, 3)
    assert bool(store.redis.sismember(store.key("open-indexed"), "receiver")) == (conflicts < 3)


def test_open_index_cleanup_lists_all_owned_keys(store):
    first = store.send("sender", "receiver", "work")
    store.open_items("receiver")
    store.pending_items("receiver")
    keys, memberships = store.keys_for(lambda address: address == "receiver")
    assert set(keys) == {
        store.key("address", "receiver"),
        store.key("pending", "receiver"),
        store.key("open", "receiver"),
        store.key("open-size", "receiver"),
        store.key("sequence", "receiver"),
        store.key("item", first.id),
        store.key("history", first.id),
    }
    assert memberships == {
        store.key("waiting"): ["receiver"],
        store.key("indexed"): ["receiver"],
        store.key("open-indexed"): ["receiver"],
    }


def test_open_index_cleanup_keeps_empty_addresses_and_redirected_mail(store):
    assert store.open_items("empty") == []
    keys, memberships = store.keys_for(lambda address: address == "empty")
    assert set(keys) == {store.key(kind, "empty") for kind in ("address", "pending", "open", "open-size", "sequence")}
    assert memberships == {store.key("open-indexed"): ["empty"]}
    item = store.send("sender", "old", "work")
    store.open_items("old")
    store.redirect(item.id, "swarm", "new", "moved", "old")
    keys, memberships = store.keys_for(lambda address: address == "old")
    assert set(keys) == {store.key(kind, "old") for kind in ("address", "pending", "open", "open-size", "sequence")}
    assert memberships == {store.key("open-indexed"): ["old"]}


@pytest.mark.parametrize("change", ["send", "close"])
def test_open_index_rebuild_observes_concurrent_mail_changes(store, monkeypatch, change):
    first = store.send("sender", "receiver", "first")
    pipeline_type = type(store.redis.pipeline())
    hgetall = pipeline_type.hgetall
    changed = []
    calls = []
    index = store._index_open

    def rebuild(address):
        calls.append(address)
        return index(address)

    def read_then_change(pipe, key):
        data = hgetall(pipe, key)
        if key == store.key("item", first.id) and not changed:
            changed.append(True)
            if change == "send":
                store.send("sender", "receiver", "second")
            else:
                store.close(first.id, "receiver", "done", "finished")
        return data

    monkeypatch.setattr(store, "_index_open", rebuild)
    monkeypatch.setattr(pipeline_type, "hgetall", read_then_change)
    opened = store.open_items("receiver")
    assert calls == ["receiver", "receiver"]
    assert [item.text for item in opened] == (["first", "second"] if change == "send" else [])
    assert all(item.state not in ("done", "blocked", "handed_off", "cancelled") for item in opened)


def test_open_index_observes_writes_from_a_channel_using_the_previous_protocol(store):
    first = store.send("sender", "receiver", "first")
    assert [item.id for item in store.open_items("receiver")] == [first.id]
    second = store.send("sender", "receiver", "second")
    store.redis.zrem(store.key("open", "receiver"), second.id)
    store.redis.decr(store.key("open-size", "receiver"))
    assert [item.id for item in store.open_items("receiver")] == [first.id, second.id]
    store.close(first.id, "receiver", "done", "finished")
    store.redis.zadd(store.key("open", "receiver"), {first.id: first.created_at})
    assert [item.id for item in store.open_items("receiver")] == [second.id]
    assert store.redis.zscore(store.key("open", "receiver"), first.id) is None


def test_open_index_cleanup_finds_a_redirected_address_before_first_read(store):
    item = store.send("sender", "old", "work")
    store.redirect(item.id, "swarm", "new", "moved", "old")
    keys, memberships = store.keys_for(lambda address: address == "old")
    assert set(keys) == {store.key(kind, "old") for kind in ("address", "pending", "open", "open-size", "sequence")}
    assert memberships == {}


@pytest.mark.parametrize("change", ["send", "redirect", "same_address"])
def test_new_writers_keep_warm_mailbox_indexes_current(store, monkeypatch, change):
    first = store.send("sender", "a", "first")
    assert [item.id for item in store.open_items("a")] == [first.id]
    assert store.open_items("b") == []

    def rebuild(address):
        pytest.fail(f"new writer invalidated warm index at {address}")

    monkeypatch.setattr(store, "_index_open", rebuild)
    if change == "send":
        second = store.send("sender", "a", "second")
        expected = ([first.id, second.id], [])
    else:
        destination = "b" if change == "redirect" else "a"
        store.redirect(first.id, "swarm", destination, "moved", "a")
        expected = ([], [first.id]) if destination == "b" else ([first.id], [])
    assert [item.id for item in store.open_items("a")] == expected[0]
    assert [item.id for item in store.open_items("b")] == expected[1]
    for address in ("a", "b"):
        assert int(store.redis.get(store.key("open-size", address))) == store.redis.zcard(store.key("address", address))


@pytest.mark.parametrize("count", [1, 5])
def test_warm_index_reads_all_open_mail_and_rebuilds_when_its_size_stamp_is_missing(store, count):
    items = [store.send("sender", "receiver", str(index)) for index in range(count)]
    expected = [item.id for item in items]
    assert [item.id for item in store.open_items("receiver")] == expected
    assert [item.id for item in store.open_items("receiver")] == expected
    store.redis.delete(store.key("open-size", "receiver"))
    store.redis.delete(store.key("open", "receiver"))
    assert [item.id for item in store.open_items("receiver")] == expected


def test_legacy_redirect_rebuild_removes_stale_open_pointers(store):
    item = store.send("sender", "old", "work")
    store.open_items("old")
    store.redirect(item.id, "swarm", "new", "moved", "old")
    store.redis.zadd(store.key("open", "old"), {item.id: item.created_at})
    store.redis.incr(store.key("open-size", "old"))
    assert store.open_items("old") == []
    assert store.redis.zcard(store.key("open", "old")) == 0
    assert [entry.id for entry in store.open_items("new")] == [item.id]


def test_cleanup_reads_legacy_addresses_and_index_members_without_size_stamps(store):
    item = store.send("sender", "legacy", "work")
    store.redis.delete(store.key("open", "legacy"), store.key("open-size", "legacy"))
    store.open_items("empty")
    store.redis.delete(store.key("open-size", "empty"))
    keys, memberships = store.keys_for(lambda address: True)
    expected = {
        store.key(kind, address)
        for address in ("empty", "legacy")
        for kind in ("address", "pending", "open", "open-size", "sequence")
    }
    expected.update({store.key("item", item.id), store.key("history", item.id)})
    assert set(keys) == expected
    assert memberships == {store.key("waiting"): ["legacy"], store.key("open-indexed"): ["empty"]}


def test_new_withdrawal_does_not_read_closed_mail_on_the_next_sweep(store, monkeypatch):
    item = store.send("sender", "receiver", "work")
    store.open_items("receiver")
    assert store.withdraw(item.id, "swarm", "receiver exited", "receiver").state == "cancelled"
    seen = []
    get = store.get

    def read(item_id):
        seen.append(item_id)
        return get(item_id)

    monkeypatch.setattr(store, "get", read)
    assert store.open_items("receiver") == []
    assert seen == []


def test_open_index_names_a_missing_item(store):
    item = store.send("sender", "receiver", "work")
    store.redis.delete(store.key("item", item.id))
    with pytest.raises(InboxError) as error:
        store.open_items("receiver")
    assert str(error.value) == f"no message {item.id}"


def test_quiet_names_only_indexed_addresses_without_open_mail(store):
    done = store.send("alice", "gone", "finished")
    store.close(done.id, "gone", "done", "finished")
    store.open_items("gone")
    store.open_items("empty")
    store.send("alice", "late", "contract")
    store.open_items("late")
    store.send("alice", "never-indexed", "contract")
    assert store.quiet(["gone", "empty", "late", "never-indexed", "unknown"]) == {"gone", "empty"}
    store.send("alice", "empty", "arrived after the index")
    assert store.quiet(["empty"]) == set()
    assert store.quiet([]) == set()
