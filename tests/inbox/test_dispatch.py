import pytest

from scripts.inbox.dispatch import REASSIGNED, RELEASED, Dispatcher, DispatchError, digest
from scripts.inbox.seen import SEEN_ON_LEDGER, SeenMarks, claim, first_showing, write_ref
from scripts.inbox.store import InboxStore, owner_key

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


@pytest.fixture
def dispatcher(store):
    found = Dispatcher(store)
    found.own("bob", "bridge-1")
    return found


def send(dispatcher, bridge, delivery):
    dispatcher.submitting(delivery.id, bridge)
    return dispatcher.accept(delivery.id, bridge, delivery.digest)


def states(store, item_id):
    return [entry["state"] for entry in store.history(item_id) if "state" in entry]


def test_own_sets_the_delivery_owner(store):
    dispatcher = Dispatcher(store)
    assert dispatcher.owner("bob") == ""
    dispatcher.own("bob", "bridge-1")
    assert dispatcher.owner("bob") == "bridge-1"
    assert store.redis.get(owner_key("bob")) == "bridge-1"


def test_own_again_by_the_same_owner_is_kept(dispatcher):
    dispatcher.own("bob", "bridge-1")
    assert dispatcher.owner("bob") == "bridge-1"


def test_a_second_bridge_is_refused_without_takeover(dispatcher):
    with pytest.raises(DispatchError) as refused:
        dispatcher.own("bob", "bridge-2")
    assert str(refused.value) == "bob is delivered by bridge-1; take over to replace it"
    assert dispatcher.owner("bob") == "bridge-1"


def test_a_second_bridge_takes_over_and_the_first_loses_every_write(store, dispatcher):
    store.send("alice", "bob", "hi")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    dispatcher.own("bob", "bridge-2", takeover=True)
    assert dispatcher.owner("bob") == "bridge-2"
    with pytest.raises(DispatchError) as refused:
        dispatcher.submitting(delivery.id, "bridge-1")
    assert str(refused.value) == "bridge-1 does not deliver for bob; bridge-2 does"
    with pytest.raises(DispatchError):
        dispatcher.reserve("bob", "bridge-1")
    with pytest.raises(DispatchError):
        dispatcher.recover("bob", "bridge-1")
    assert dispatcher.get(delivery.id).state == "reserved"


def test_release_by_another_owner_keeps_the_owner(dispatcher):
    assert dispatcher.release("bob", "bridge-2") is False
    assert dispatcher.owner("bob") == "bridge-1"
    assert dispatcher.release("bob", "bridge-1") is True
    assert dispatcher.owner("bob") == ""


def test_released_recipient_takes_legacy_claim_again(store, dispatcher):
    item = store.send("alice", "bob", "hi")
    dispatcher.release("bob", "bridge-1")
    assert [shown.id for shown in claim(store, "bob")] == [item.id]


def test_reserve_takes_pending_items_in_inbox_order_with_a_digest(store, dispatcher):
    first = store.send("alice", "bob", "one")
    second = store.send("carol", "bob", "two", ref="sw:3:c1")
    reserved = dispatcher.reserve("bob", "bridge-1")
    assert [(d.item, d.recipient, d.owner, d.state, d.ref) for d in reserved] == [
        (first.id, "bob", "bridge-1", "reserved", ""),
        (second.id, "bob", "bridge-1", "reserved", "sw:3:c1"),
    ]
    assert [d.digest for d in reserved] == [digest(first), digest(second)]
    assert len({d.id for d in reserved}) == 2
    assert dispatcher.get(reserved[0].id) == reserved[0]
    assert store.get(first.id).state == "pending"


def test_reserve_by_a_non_owner_is_refused(store, dispatcher):
    store.send("alice", "bob", "hi")
    with pytest.raises(DispatchError):
        dispatcher.reserve("bob", "bridge-2")


def test_a_repeated_notify_reserves_nothing_new(store, dispatcher):
    store.send("alice", "bob", "hi")
    assert len(dispatcher.reserve("bob", "bridge-1")) == 1
    assert dispatcher.reserve("bob", "bridge-1") == []
    later = store.send("alice", "bob", "again")
    assert [d.item for d in dispatcher.reserve("bob", "bridge-1")] == [later.id]


def test_digest_covers_the_payload(store):
    item = store.send("alice", "bob", "hi")
    other = store.send("alice", "bob", "hi")
    assert digest(item) != digest(other)
    assert digest(item) == digest(store.get(item.id))
    assert len(digest(item)) == 64


def test_accept_commits_delivered_and_the_seen_mark(store, dispatcher):
    item = store.send("alice", "bob", "hi", ref="sw:3:c1")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    submitting = dispatcher.submitting(delivery.id, "bridge-1")
    assert submitting.state == "submitting"
    assert SeenMarks(store.redis).seen("bob", "sw:3:c1") is False
    committed = dispatcher.accept(delivery.id, "bridge-1", delivery.digest)
    assert (committed.state, committed.committed) == ("accepted", True)
    assert store.get(item.id).state == "delivered"
    assert states(store, item.id) == ["pending", "delivered"]
    assert store.history(item.id)[-1]["by"] == "bob"
    assert SeenMarks(store.redis).seen("bob", "sw:3:c1") is True
    assert store.redis.ttl(SeenMarks(store.redis).key("bob")) > 0
    assert store.pending_items("bob") == []
    assert dispatcher.reserve("bob", "bridge-1") == []


def test_accept_without_a_ref_writes_no_seen_mark(store, dispatcher):
    store.send("alice", "bob", "hi")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    send(dispatcher, "bridge-1", delivery)
    assert not store.redis.exists(SeenMarks(store.redis).key("bob"))


def test_accept_needs_a_submission(store, dispatcher):
    store.send("alice", "bob", "hi")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    with pytest.raises(DispatchError) as refused:
        dispatcher.accept(delivery.id, "bridge-1", delivery.digest)
    assert str(refused.value) == f"delivery {delivery.id} is reserved"


def test_a_repeated_accept_is_refused_and_delivers_once(store, dispatcher):
    item = store.send("alice", "bob", "hi")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    send(dispatcher, "bridge-1", delivery)
    with pytest.raises(DispatchError):
        dispatcher.accept(delivery.id, "bridge-1", delivery.digest)
    assert states(store, item.id) == ["pending", "delivered"]


def test_an_unknown_delivery_is_refused(dispatcher):
    with pytest.raises(DispatchError) as refused:
        dispatcher.submitting("nope", "bridge-1")
    assert str(refused.value) == "no delivery nope"


def test_an_invalid_payload_is_rejected_and_released(store, dispatcher):
    item = store.send("alice", "bob", "hi", ref="sw:3:c1")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    dispatcher.submitting(delivery.id, "bridge-1")
    rejected = dispatcher.accept(delivery.id, "bridge-1", "0" * 64)
    assert (rejected.state, rejected.reason) == ("rejected", "the accepted payload does not match the reserved one")
    assert store.get(item.id).state == "pending"
    assert SeenMarks(store.redis).seen("bob", "sw:3:c1") is False
    [again] = dispatcher.reserve("bob", "bridge-1")
    assert (again.item, again.ref) == (item.id, "sw:3:c1")
    assert again.id != delivery.id


def test_reject_releases_the_item(store, dispatcher):
    item = store.send("alice", "bob", "hi")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    rejected = dispatcher.reject(delivery.id, "bridge-1", "turn refused")
    assert (rejected.state, rejected.reason) == ("rejected", "turn refused")
    assert [d.item for d in dispatcher.reserve("bob", "bridge-1")] == [item.id]
    with pytest.raises(DispatchError):
        dispatcher.reject(delivery.id, "bridge-1", "again")


def test_crash_before_write_releases_the_reservation_on_recovery(store, dispatcher):
    item = store.send("alice", "bob", "hi")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    [recovered] = dispatcher.recover("bob", "bridge-1")
    assert (recovered.id, recovered.state, recovered.reason) == (delivery.id, "rejected", RELEASED)
    [again] = dispatcher.reserve("bob", "bridge-1")
    assert again.item == item.id
    assert dispatcher.recover("bob", "bridge-1") == [dispatcher.get(again.id)]


def test_crash_after_write_turns_unknown_and_never_sends_twice(store, dispatcher):
    item = store.send("alice", "bob", "hi")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    dispatcher.submitting(delivery.id, "bridge-1")
    [recovered] = dispatcher.recover("bob", "bridge-1")
    assert recovered.state == "unknown"
    assert dispatcher.reserve("bob", "bridge-1") == []
    assert dispatcher.recover("bob", "bridge-1") == [recovered]
    committed = dispatcher.accept(delivery.id, "bridge-1", delivery.digest)
    assert committed.committed is True
    assert store.get(item.id).state == "delivered"


def test_crash_after_acceptance_is_committed_on_recovery(store, dispatcher, monkeypatch):
    item = store.send("alice", "bob", "hi", ref="sw:3:c1")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    dispatcher.submitting(delivery.id, "bridge-1")

    def crash(*_):
        raise SystemExit("crashed")

    monkeypatch.setattr(dispatcher, "_commit", crash)
    with pytest.raises(SystemExit):
        dispatcher.accept(delivery.id, "bridge-1", delivery.digest)
    monkeypatch.undo()
    assert (dispatcher.get(delivery.id).state, dispatcher.get(delivery.id).committed) == ("accepted", False)
    assert store.get(item.id).state == "pending"
    [recovered] = Dispatcher(store).recover("bob", "bridge-1")
    assert (recovered.state, recovered.committed) == ("accepted", True)
    assert states(store, item.id) == ["pending", "delivered"]
    assert SeenMarks(store.redis).seen("bob", "sw:3:c1") is True
    assert dispatcher.recover("bob", "bridge-1") == []


def test_crash_before_commit_writes_nothing_and_recovery_commits_once(store, dispatcher, monkeypatch):
    item = store.send("alice", "bob", "hi", ref="sw:3:c1")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    dispatcher.submitting(delivery.id, "bridge-1")
    real = store.stage_move

    def crash(*_):
        raise SystemExit("crashed")

    monkeypatch.setattr(store, "stage_move", crash)
    with pytest.raises(SystemExit):
        dispatcher.accept(delivery.id, "bridge-1", delivery.digest)
    assert store.get(item.id).state == "pending"
    assert SeenMarks(store.redis).seen("bob", "sw:3:c1") is False
    monkeypatch.setattr(store, "stage_move", real)
    dispatcher.recover("bob", "bridge-1")
    assert states(store, item.id) == ["pending", "delivered"]
    assert SeenMarks(store.redis).seen("bob", "sw:3:c1") is True


def test_a_takeover_recovers_the_previous_owners_deliveries(store, dispatcher):
    reserved_item = store.send("alice", "bob", "one")
    sent_item = store.send("alice", "bob", "two")
    reserved, sent = dispatcher.reserve("bob", "bridge-1")
    dispatcher.submitting(sent.id, "bridge-1")
    dispatcher.own("bob", "bridge-2", takeover=True)
    recovered = {d.item: d.state for d in dispatcher.recover("bob", "bridge-2")}
    assert recovered == {reserved_item.id: "rejected", sent_item.id: "unknown"}
    [again] = dispatcher.reserve("bob", "bridge-2")
    assert (again.item, again.owner) == (reserved_item.id, "bridge-2")
    assert dispatcher.accept(sent.id, "bridge-2", sent.digest).committed is True


def test_hook_delivery_is_refused_while_an_owner_holds_the_recipient(store, dispatcher):
    item = store.send("alice", "bob", "hi")
    assert store.deliver(item.id, "bob") is None
    assert claim(store, "bob") == []
    assert store.get(item.id).state == "pending"
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    assert delivery.item == item.id


def test_hook_delivery_reads_the_owner_inside_its_transaction(store, dispatcher):
    dispatcher.release("bob", "bridge-1")
    store.send("alice", "bob", "hi")
    pending = store.pending_mail("bob")
    Dispatcher(store).own("bob", "bridge-1")
    assert [store.deliver(found.id, "bob") for found in pending] == [None]


def test_other_recipients_keep_legacy_delivery(store, dispatcher):
    item = store.send("alice", "carol", "hi")
    assert store.deliver(item.id, "carol").state == "delivered"


def test_seen_marks_are_refused_while_an_owner_holds_the_name(store, dispatcher):
    marks = SeenMarks(store.redis)
    event = {"rev": 3, "id": "c1"}
    assert first_showing(marks, "bob", "sw", [event]) == []
    assert marks.seen("bob", write_ref("sw", event)) is False
    assert marks.mark("carol", "sw:3:c1") is True
    dispatcher.release("bob", "bridge-1")
    assert first_showing(marks, "bob", "sw", [event]) == [event]


def test_a_repeated_ref_waits_then_is_superseded(store, dispatcher):
    first = store.send("operator", "bob", "comment", ref="sw:3:c1")
    second = store.send("operator", "bob", "comment again", ref="sw:3:c1")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    assert delivery.item == first.id
    assert dispatcher.reserve("bob", "bridge-1") == []
    send(dispatcher, "bridge-1", delivery)
    assert dispatcher.reserve("bob", "bridge-1") == []
    closed = store.get(second.id)
    assert (closed.state, closed.reason) == ("done", f"done: {SEEN_ON_LEDGER}")


def test_a_ref_held_by_an_accepted_delivery_supersedes(store, dispatcher, monkeypatch):
    store.send("operator", "bob", "comment", ref="sw:3:c1")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    dispatcher.submitting(delivery.id, "bridge-1")
    monkeypatch.setattr(dispatcher, "_commit", lambda *_: None)
    dispatcher.accept(delivery.id, "bridge-1", delivery.digest)
    monkeypatch.undo()
    second = store.send("operator", "bob", "comment again", ref="sw:3:c1")
    assert dispatcher.reserve("bob", "bridge-1") == []
    assert store.get(second.id).state == "done"


def test_a_ref_rejected_frees_the_next_item(store, dispatcher):
    first = store.send("operator", "bob", "comment", ref="sw:3:c1")
    second = store.send("operator", "bob", "comment again", ref="sw:3:c1")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    dispatcher.reject(delivery.id, "bridge-1", "turn refused")
    assert [d.item for d in dispatcher.reserve("bob", "bridge-1")] == [first.id]
    assert store.get(second.id).state == "pending"


def test_a_ref_shown_on_the_ledger_before_owning_supersedes(store):
    SeenMarks(store.redis).mark("bob", "sw:3:c1")
    item = store.send("operator", "bob", "comment", ref="sw:3:c1")
    dispatcher = Dispatcher(store)
    dispatcher.own("bob", "bridge-1")
    assert dispatcher.reserve("bob", "bridge-1") == []
    assert store.get(item.id).state == "done"


def test_seat_items_are_reserved_for_the_occupant(store):
    store.seats.occupy("eng-1@sw", "bob", 1)
    item = store.send("alice", "eng-1@sw", "hi")
    dispatcher = Dispatcher(store)
    dispatcher.own("bob", "bridge-1")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    assert send(dispatcher, "bridge-1", delivery).committed is True
    assert store.get(item.id).state == "delivered"


def test_seat_reassignment_supersedes_and_releases(store):
    store.seats.occupy("eng-1@sw", "bob", 1)
    item = store.send("alice", "eng-1@sw", "hi", ref="sw:3:c1")
    dispatcher = Dispatcher(store)
    dispatcher.own("bob", "bridge-1")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    dispatcher.submitting(delivery.id, "bridge-1")
    store.seats.occupy("eng-1@sw", "carol", 2)
    superseded = dispatcher.accept(delivery.id, "bridge-1", delivery.digest)
    assert (superseded.state, superseded.reason) == ("superseded", REASSIGNED)
    assert store.get(item.id).state == "pending"
    assert SeenMarks(store.redis).seen("bob", "sw:3:c1") is False
    assert dispatcher.recover("bob", "bridge-1") == []
    assert [shown.id for shown in claim(store, "carol")] == [item.id]


def test_an_item_closed_while_reserved_is_superseded(store, dispatcher):
    item = store.send("alice", "bob", "hi")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    dispatcher.submitting(delivery.id, "bridge-1")
    store.close(item.id, "bob", "cancel")
    assert dispatcher.accept(delivery.id, "bridge-1", delivery.digest).state == "superseded"
    assert store.get(item.id).state == "cancelled"


def test_reserve_skips_an_item_that_left_pending(store, dispatcher, monkeypatch):
    item = store.send("alice", "bob", "hi")
    listed = store.pending_mail("bob")
    store.close(item.id, "bob", "cancel")
    monkeypatch.setattr(store, "pending_mail", lambda _: listed)
    assert dispatcher.reserve("bob", "bridge-1") == []


def test_a_concurrent_change_retries_the_transaction(store, dispatcher, monkeypatch):
    from redis.exceptions import WatchError

    item = store.send("alice", "bob", "hi")
    calls = []
    real = dispatcher._reserve

    def flaky(pipe, *args):
        calls.append(1)
        if len(calls) == 1:
            raise WatchError("changed")
        return real(pipe, *args)

    monkeypatch.setattr(dispatcher, "_reserve", flaky)
    assert [d.item for d in dispatcher.reserve("bob", "bridge-1")] == [item.id]
    assert len(calls) == 2


def test_a_transaction_that_keeps_changing_is_refused(store, dispatcher, monkeypatch):
    from redis.exceptions import WatchError

    store.send("alice", "bob", "hi")

    def always(*_):
        raise WatchError("changed")

    monkeypatch.setattr(dispatcher, "_reserve", always)
    with pytest.raises(DispatchError) as refused:
        dispatcher.reserve("bob", "bridge-1")
    assert str(refused.value) == "the inbox changed meanwhile; run it again"
