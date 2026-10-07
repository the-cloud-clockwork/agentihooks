import pytest

from scripts.inbox.store import InboxError, InboxStore

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
    assert all(store.redis.ttl(key) == -1 for key in store.redis.keys("agentihooks:inbox:*"))
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
