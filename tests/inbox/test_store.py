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
        ("done", "", "done", "done"),
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
        store.close(item.id, "mallory", "done")
    assert store.close(item.id, "alice", "cancel").state == "cancelled"


def test_a_closed_item_stays_closed(store):
    item = store.send("alice", "bob", "hi")
    store.close(item.id, "bob", "done")
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
