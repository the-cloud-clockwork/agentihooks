import hashlib
import json

import pytest

from scripts.inbox.dispatch import SHOWN, Dispatcher, digest
from scripts.inbox.receipts import REASSIGNED, RELEASED, DispatchError, Receipts
from scripts.inbox.seen import SEEN_ON_LEDGER, SeenMarks, claim, first_showing, write_ref
from scripts.inbox.store import OWNER_TTL_ENV, InboxStore, now_ms, owner_key, owner_ttl_s
from scripts.swarm.naming import NameRegistry

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
    dispatcher.receipts.submitting(delivery.id, bridge)
    return dispatcher.receipts.accept(delivery.id, bridge, delivery.digest)


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
        dispatcher.receipts.submitting(delivery.id, "bridge-1")
    assert str(refused.value) == "bridge-1 does not deliver for bob; bridge-2 does"
    with pytest.raises(DispatchError) as refused:
        dispatcher.reserve("bob", "bridge-1")
    assert str(refused.value) == "bridge-1 does not deliver for bob; bridge-2 does"
    with pytest.raises(DispatchError) as refused:
        dispatcher.receipts.recover("bob", "bridge-1")
    assert str(refused.value) == "bridge-1 does not deliver for bob; bridge-2 does"
    assert dispatcher.receipts.get(delivery.id).state == "reserved"


def test_release_by_another_owner_keeps_the_owner(dispatcher):
    assert dispatcher.release("bob", "bridge-2") is False
    assert dispatcher.owner("bob") == "bridge-1"
    assert dispatcher.release("bob", "bridge-1") is True
    assert dispatcher.owner("bob") == ""


def test_released_recipient_takes_legacy_claim_again(store, dispatcher):
    item = store.send("alice", "bob", "hi")
    dispatcher.release("bob", "bridge-1")
    assert [shown.id for shown in claim(store, "bob")] == [item.id]


@pytest.fixture
def clock(monkeypatch):
    import time

    real = time.time
    offset = [0.0]
    monkeypatch.setattr(time, "time", lambda: real() + offset[0])

    def advance(seconds):
        offset[0] += seconds

    return advance


def test_the_owner_expiry_defaults_to_thirty_seconds():
    assert OWNER_TTL_ENV == "AGENTIHOOKS_INBOX_OWNER_TTL_S"
    assert owner_ttl_s({}) == 30
    assert owner_ttl_s({OWNER_TTL_ENV: "5"}) == 5
    assert owner_ttl_s({OWNER_TTL_ENV: "1"}) == 1
    assert owner_ttl_s({OWNER_TTL_ENV: "0"}) == 1
    assert owner_ttl_s({OWNER_TTL_ENV: "-4"}) == 1


def test_own_holds_the_recipient_for_the_owner_expiry(store, dispatcher):
    assert store.redis.ttl(owner_key("bob")) == 30


def test_own_and_renew_read_the_expiry_from_the_environment(store, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_INBOX_OWNER_TTL_S", "7")
    dispatcher = Dispatcher(store)
    dispatcher.own("bob", "bridge-1")
    assert store.redis.ttl(owner_key("bob")) == 7
    store.redis.expire(owner_key("bob"), 2)
    assert dispatcher.renew("bob", "bridge-1") is True
    assert store.redis.ttl(owner_key("bob")) == 7


def test_renew_resolves_an_alias(store, dispatcher, clock):
    store.redis.set(NameRegistry.key("alias", "old-name"), "bob")
    clock(10)
    assert dispatcher.renew("old-name", "bridge-1") is True
    assert store.redis.ttl(owner_key("bob")) == 30


def test_an_owner_taken_inside_the_renew_transaction_is_not_extended(store, dispatcher, monkeypatch):
    import scripts.inbox.dispatch as dispatch

    taken = []

    def take_over_and_return_ttl():
        if not taken:
            taken.append(1)
            store.redis.set(owner_key("bob"), "bridge-2", ex=5)
        return 30

    monkeypatch.setattr(dispatch, "owner_ttl_s", take_over_and_return_ttl)
    assert dispatcher.renew("bob", "bridge-1") is False
    assert dispatcher.owner("bob") == "bridge-2"
    assert store.redis.ttl(owner_key("bob")) == 5


def test_a_takeover_holds_the_recipient_for_the_owner_expiry(store, dispatcher, clock):
    clock(20)
    dispatcher.own("bob", "bridge-2", takeover=True)
    assert store.redis.ttl(owner_key("bob")) == 30


def test_a_killed_bridge_releases_its_recipient_within_the_expiry_window(store, dispatcher, clock):
    store.send("alice", "bob", "before")
    assert claim(store, "bob") == []
    clock(29)
    assert dispatcher.owner("bob") == "bridge-1"
    clock(2)
    after = store.send("alice", "bob", "after")
    assert dispatcher.owner("bob") == ""
    assert after.id in [shown.id for shown in claim(store, "bob")]
    successor = Dispatcher(store)
    successor.own("bob", "bridge-2")
    assert successor.owner("bob") == "bridge-2"


def test_a_killed_bridge_keeps_its_in_flight_items_for_a_successor(store, dispatcher, clock):
    first = store.send("alice", "bob", "submitted")
    second = store.send("alice", "bob", "reserved")
    submitted, _ = dispatcher.reserve("bob", "bridge-1")
    dispatcher.receipts.submitting(submitted.id, "bridge-1")
    clock(31)
    after = store.send("alice", "bob", "after")
    assert [shown.id for shown in claim(store, "bob")] == [second.id, after.id]
    assert store.get(first.id).state == "pending"
    successor = Dispatcher(store)
    successor.own("bob", "bridge-2")
    recovered = {d.item: (d.state, d.reason) for d in successor.receipts.recover("bob", "bridge-2")}
    assert recovered == {first.id: ("unknown", ""), second.id: ("rejected", RELEASED)}
    assert successor.reserve("bob", "bridge-2") == []


def test_a_lapsed_bridge_cannot_submit_an_item_the_hook_path_delivered(store, dispatcher, clock):
    item = store.send("alice", "bob", "hi")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    clock(31)
    assert [shown.id for shown in claim(store, "bob")] == [item.id]
    dispatcher.own("bob", "bridge-1")
    with pytest.raises(DispatchError) as refused:
        dispatcher.receipts.submitting(delivery.id, "bridge-1")
    assert str(refused.value) == f"message {item.id} is delivered for bob, not bob's to submit"
    assert dispatcher.receipts.get(delivery.id).state == "reserved"


def test_a_redirected_item_is_not_submitted_to_its_old_recipient(store, dispatcher):
    item = store.send("alice", "bob", "hi")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    store.redirect(item.id, "swarm", "carol", "moved")
    with pytest.raises(DispatchError) as refused:
        dispatcher.receipts.submitting(delivery.id, "bridge-1")
    assert str(refused.value) == f"message {item.id} is pending for carol, not bob's to submit"
    assert dispatcher.receipts.get(delivery.id).state == "reserved"


def test_a_killed_bridge_lets_the_ledger_marks_through_again(store, dispatcher, clock):
    marks = SeenMarks(store.redis)
    event = {"rev": 3, "id": "c1"}
    assert first_showing(marks, "bob", "sw", [event]) == []
    clock(31)
    assert first_showing(marks, "bob", "sw", [event]) == [event]


def test_a_renewing_bridge_keeps_its_recipient_past_the_first_expiry(store, dispatcher, clock):
    clock(20)
    assert dispatcher.renew("bob", "bridge-1") is True
    assert store.redis.ttl(owner_key("bob")) == 30
    clock(20)
    store.send("alice", "bob", "hi")
    assert dispatcher.owner("bob") == "bridge-1"
    assert claim(store, "bob") == []


def test_renew_by_another_bridge_changes_nothing(store, dispatcher, clock):
    clock(10)
    assert dispatcher.renew("bob", "bridge-2") is False
    assert dispatcher.owner("bob") == "bridge-1"
    assert store.redis.ttl(owner_key("bob")) == 20


def test_renew_after_the_expiry_never_brings_the_owner_back(store, dispatcher, clock):
    clock(31)
    assert dispatcher.renew("bob", "bridge-1") is False
    assert store.redis.exists(owner_key("bob")) == 0
    assert dispatcher.owner("bob") == ""


def test_renew_watches_the_owner(store, dispatcher, watched):
    watched.clear()
    dispatcher.renew("bob", "bridge-1")
    assert set(watched) == {owner_key("bob")}


def test_hook_delivery_watches_the_reservation_once_the_owner_expired(store, dispatcher, clock, watched):
    item = store.send("alice", "bob", "hi")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    dispatcher.receipts.submitting(delivery.id, "bridge-1")
    clock(31)
    watched.clear()
    assert store.deliver(item.id, "bob") is None
    assert {store.key("reservation", item.id), store.key("delivery", delivery.id)} <= set(watched)


def test_submitting_watches_the_item(store, dispatcher, watched):
    item = store.send("alice", "bob", "hi")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    watched.clear()
    dispatcher.receipts.submitting(delivery.id, "bridge-1")
    assert store.key("item", item.id) in watched


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
    assert dispatcher.receipts.get(reserved[0].id) == reserved[0]
    assert store.get(first.id).state == "pending"


def test_reserve_by_a_non_owner_is_refused(store, dispatcher):
    store.send("alice", "bob", "hi")
    with pytest.raises(DispatchError) as refused:
        dispatcher.reserve("bob", "bridge-2")
    assert str(refused.value) == "bridge-2 does not deliver for bob; bridge-1 does"


def test_recover_by_a_non_owner_names_nobody_once_released(store):
    with pytest.raises(DispatchError) as refused:
        Receipts(store).recover("bob", "bridge-1")
    assert str(refused.value) == "bridge-1 does not deliver for bob; nobody does"


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
    submitting = dispatcher.receipts.submitting(delivery.id, "bridge-1")
    assert submitting.state == "submitting"
    assert SeenMarks(store.redis).seen("bob", "sw:3:c1") is False
    committed = dispatcher.receipts.accept(delivery.id, "bridge-1", delivery.digest)
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
        dispatcher.receipts.accept(delivery.id, "bridge-1", delivery.digest)
    assert str(refused.value) == f"delivery {delivery.id} is reserved"


def test_a_repeated_accept_is_refused_and_delivers_once(store, dispatcher):
    item = store.send("alice", "bob", "hi")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    send(dispatcher, "bridge-1", delivery)
    with pytest.raises(DispatchError) as refused:
        dispatcher.receipts.accept(delivery.id, "bridge-1", delivery.digest)
    assert str(refused.value) == f"delivery {delivery.id} is accepted"
    assert states(store, item.id) == ["pending", "delivered"]


def test_an_unknown_delivery_is_refused(dispatcher):
    with pytest.raises(DispatchError) as refused:
        dispatcher.receipts.submitting("nope", "bridge-1")
    assert str(refused.value) == "no delivery nope"


def test_an_invalid_payload_is_rejected_and_released(store, dispatcher):
    item = store.send("alice", "bob", "hi", ref="sw:3:c1")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    dispatcher.receipts.submitting(delivery.id, "bridge-1")
    rejected = dispatcher.receipts.accept(delivery.id, "bridge-1", "0" * 64)
    assert (rejected.state, rejected.reason) == ("rejected", "the accepted payload does not match the reserved one")
    assert store.get(item.id).state == "pending"
    assert SeenMarks(store.redis).seen("bob", "sw:3:c1") is False
    [again] = dispatcher.reserve("bob", "bridge-1")
    assert (again.item, again.ref) == (item.id, "sw:3:c1")
    assert again.id != delivery.id


def test_reject_releases_the_item(store, dispatcher):
    item = store.send("alice", "bob", "hi")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    rejected = dispatcher.receipts.reject(delivery.id, "bridge-1", "turn refused")
    assert (rejected.state, rejected.reason) == ("rejected", "turn refused")
    assert [d.item for d in dispatcher.reserve("bob", "bridge-1")] == [item.id]
    with pytest.raises(DispatchError) as refused:
        dispatcher.receipts.reject(delivery.id, "bridge-1", "again")
    assert str(refused.value) == f"delivery {delivery.id} is rejected"


def test_release_is_refused_while_a_delivery_is_open(store, dispatcher):
    item = store.send("alice", "bob", "hi")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    dispatcher.receipts.submitting(delivery.id, "bridge-1")
    with pytest.raises(DispatchError) as refused:
        dispatcher.release("bob", "bridge-1")
    assert str(refused.value) == "bob still has open deliveries; accept or reject them before releasing"
    assert dispatcher.owner("bob") == "bridge-1"
    assert claim(store, "bob") == []
    assert dispatcher.release("bob", "bridge-2") is False
    dispatcher.receipts.reject(delivery.id, "bridge-1", "turn refused")
    assert dispatcher.release("bob", "bridge-1") is True
    assert [shown.id for shown in claim(store, "bob")] == [item.id]


def test_a_superseded_item_closes_in_the_reserve_transaction(store, dispatcher):
    store.redis.sadd(SeenMarks(store.redis).key("bob"), "sw:3:c1")
    shown = store.send("operator", "bob", "comment", ref="sw:3:c1")
    fresh_item = store.send("alice", "bob", "hi")
    assert [d.item for d in dispatcher.reserve("bob", "bridge-1")] == [fresh_item.id]
    closed = store.get(shown.id)
    assert (closed.state, closed.reason) == ("done", f"done: {SEEN_ON_LEDGER}")
    assert store.history(shown.id)[-1]["by"] == "bob"
    assert [item.id for item in store.pending_items("bob")] == [fresh_item.id]


def test_every_item_with_a_shown_ref_closes_in_one_pass(store, dispatcher):
    store.redis.sadd(SeenMarks(store.redis).key("bob"), "sw:3:c1")
    first = store.send("operator", "bob", "comment", ref="sw:3:c1")
    second = store.send("operator", "bob", "comment again", ref="sw:3:c1")
    assert dispatcher.reserve("bob", "bridge-1") == []
    assert [store.get(item.id).state for item in (first, second)] == ["done", "done"]


def test_the_last_superseded_item_leaves_the_waiting_set(store, dispatcher):
    store.redis.sadd(SeenMarks(store.redis).key("bob"), "sw:3:c1")
    store.send("operator", "bob", "comment", ref="sw:3:c1")
    assert dispatcher.reserve("bob", "bridge-1") == []
    assert not store.redis.sismember(store.key("waiting"), "bob")


def test_an_item_redirected_away_is_not_reserved(store, dispatcher, monkeypatch):
    item = store.send("alice", "bob", "hi")
    listed = store.pending_mail("bob")
    store.redirect(item.id, "swarm", "carol", "moved")
    monkeypatch.setattr(store, "pending_mail", lambda _: listed)
    assert dispatcher.reserve("bob", "bridge-1") == []
    assert store.get(item.id).state == "pending"


def test_a_crash_inside_reserve_writes_nothing(store, dispatcher, monkeypatch):
    from scripts.inbox import dispatch

    item = store.send("alice", "bob", "hi")

    def crash(_):
        raise SystemExit("crashed")

    monkeypatch.setattr(dispatch, "digest", crash)
    with pytest.raises(SystemExit):
        dispatcher.reserve("bob", "bridge-1")
    monkeypatch.undo()
    assert not store.redis.exists(store.key("reservation", item.id))
    assert not store.redis.exists(store.key("deliveries", "bob"))
    assert [d.item for d in dispatcher.reserve("bob", "bridge-1")] == [item.id]


def test_claim_returns_an_item_to_pending_when_its_seen_mark_keeps_changing(store, monkeypatch):
    from scripts.inbox import seen
    from scripts.inbox.store import InboxError

    raced = store.send("operator", "bob", "comment", ref="sw:3:c1")
    plain = store.send("alice", "bob", "hi")

    def contended(self, name, ref):
        raise InboxError("changed")

    monkeypatch.setattr(seen.SeenMarks, "mark", contended)
    assert [item.id for item in claim(store, "bob")] == [plain.id]
    requeued = store.get(raced.id)
    assert (requeued.state, requeued.reason) == ("pending", seen.UNSETTLED)


def test_a_seen_mark_retries_when_the_owner_changes_meanwhile(store, monkeypatch):
    from redis.exceptions import WatchError

    marks = SeenMarks(store.redis)
    calls = []
    real = store.redis.pipeline

    def pipeline(*args, **kwargs):
        pipe = real(*args, **kwargs)
        calls.append(1)
        if len(calls) == 1:
            pipe.execute = lambda: (_ for _ in ()).throw(WatchError("changed"))
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", pipeline)
    assert marks.mark("bob", "sw:3:c1") is True
    assert len(calls) == 2


def test_a_seen_mark_that_keeps_changing_is_refused(store, monkeypatch):
    from redis.exceptions import WatchError

    from scripts.inbox.store import InboxError

    real = store.redis.pipeline

    def pipeline(*args, **kwargs):
        pipe = real(*args, **kwargs)
        pipe.execute = lambda: (_ for _ in ()).throw(WatchError("changed"))
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", pipeline)
    with pytest.raises(InboxError) as refused:
        SeenMarks(store.redis).mark("bob", "sw:3:c1")
    assert str(refused.value) == "the delivery owner of bob changed meanwhile; run it again"
    assert SeenMarks(store.redis).seen("bob", "sw:3:c1") is False


def test_crash_before_write_releases_the_reservation_on_recovery(store, dispatcher):
    item = store.send("alice", "bob", "hi")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    [recovered] = dispatcher.receipts.recover("bob", "bridge-1")
    assert (recovered.id, recovered.state, recovered.reason) == (delivery.id, "rejected", RELEASED)
    [again] = dispatcher.reserve("bob", "bridge-1")
    assert again.item == item.id
    assert dispatcher.receipts.recover("bob", "bridge-1") == [dispatcher.receipts.get(again.id)]


def test_crash_after_write_turns_unknown_and_never_sends_twice(store, dispatcher):
    item = store.send("alice", "bob", "hi")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    dispatcher.receipts.submitting(delivery.id, "bridge-1")
    [recovered] = dispatcher.receipts.recover("bob", "bridge-1")
    assert recovered.state == "unknown"
    assert dispatcher.reserve("bob", "bridge-1") == []
    assert dispatcher.receipts.recover("bob", "bridge-1") == [recovered]
    committed = dispatcher.receipts.accept(delivery.id, "bridge-1", delivery.digest)
    assert committed.committed is True
    assert store.get(item.id).state == "delivered"


def test_crash_after_acceptance_is_committed_on_recovery(store, dispatcher, monkeypatch):
    item = store.send("alice", "bob", "hi", ref="sw:3:c1")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    dispatcher.receipts.submitting(delivery.id, "bridge-1")

    def crash(*_):
        raise SystemExit("crashed")

    monkeypatch.setattr(dispatcher.receipts, "_commit", crash)
    with pytest.raises(SystemExit):
        dispatcher.receipts.accept(delivery.id, "bridge-1", delivery.digest)
    monkeypatch.undo()
    assert (dispatcher.receipts.get(delivery.id).state, dispatcher.receipts.get(delivery.id).committed) == (
        "accepted",
        False,
    )
    assert store.get(item.id).state == "pending"
    [recovered] = Receipts(store).recover("bob", "bridge-1")
    assert (recovered.state, recovered.committed) == ("accepted", True)
    assert states(store, item.id) == ["pending", "delivered"]
    assert SeenMarks(store.redis).seen("bob", "sw:3:c1") is True
    assert dispatcher.receipts.recover("bob", "bridge-1") == []


def test_crash_before_commit_writes_nothing_and_recovery_commits_once(store, dispatcher, monkeypatch):
    item = store.send("alice", "bob", "hi", ref="sw:3:c1")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    dispatcher.receipts.submitting(delivery.id, "bridge-1")
    real = store.stage_move

    def crash(*_, **__):
        raise SystemExit("crashed")

    monkeypatch.setattr(store, "stage_move", crash)
    with pytest.raises(SystemExit):
        dispatcher.receipts.accept(delivery.id, "bridge-1", delivery.digest)
    assert store.get(item.id).state == "pending"
    assert SeenMarks(store.redis).seen("bob", "sw:3:c1") is False
    monkeypatch.setattr(store, "stage_move", real)
    dispatcher.receipts.recover("bob", "bridge-1")
    assert states(store, item.id) == ["pending", "delivered"]
    assert SeenMarks(store.redis).seen("bob", "sw:3:c1") is True


def test_a_takeover_recovers_the_previous_owners_deliveries(store, dispatcher):
    reserved_item = store.send("alice", "bob", "one")
    sent_item = store.send("alice", "bob", "two")
    reserved, sent = dispatcher.reserve("bob", "bridge-1")
    dispatcher.receipts.submitting(sent.id, "bridge-1")
    dispatcher.own("bob", "bridge-2", takeover=True)
    recovered = {d.item: d.state for d in dispatcher.receipts.recover("bob", "bridge-2")}
    assert recovered == {reserved_item.id: "rejected", sent_item.id: "unknown"}
    [again] = dispatcher.reserve("bob", "bridge-2")
    assert (again.item, again.owner) == (reserved_item.id, "bridge-2")
    assert dispatcher.receipts.accept(sent.id, "bridge-2", sent.digest).committed is True


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


def test_an_owner_taken_inside_the_hook_transaction_refuses_the_delivery(server, store, monkeypatch):
    item = store.send("alice", "bob", "hi")
    other = Dispatcher(fresh(server))
    real = store.last_pending
    calls = []

    def interleaved(pipe, found):
        calls.append(1)
        last = real(pipe, found)
        other.own("bob", "bridge-1")
        return last

    monkeypatch.setattr(store, "last_pending", interleaved)
    assert store.deliver(item.id, "bob") is None
    assert len(calls) == 1
    assert store.get(item.id).state == "pending"


def test_an_owner_taken_inside_the_mark_transaction_refuses_the_mark(store, monkeypatch):
    marks = SeenMarks(store.redis)
    real = store.redis.pipeline
    calls = []

    def pipeline(*args, **kwargs):
        pipe = real(*args, **kwargs)
        if not calls:
            multi = pipe.multi

            def interleaved():
                store.redis.set(owner_key("bob"), "bridge-1")
                multi()

            pipe.multi = interleaved
        calls.append(1)
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", pipeline)
    assert marks.mark("bob", "sw:3:c1") is False
    assert len(calls) == 2
    assert marks.seen("bob", "sw:3:c1") is False


def test_seen_marks_check_the_owner_of_the_resolved_name(store):
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    swarm = RedisStore(store.redis)
    swarm.create(SwarmConfig("sw", "/repo", 2, 1))
    name = swarm.next_name("sw", "eng")
    swarm.put_agent("sw", AgentRecord(name=name, lane="eng", task="t1"))
    store.names.alias("old-name", name)
    Dispatcher(store).own(name, "bridge-1")
    assert SeenMarks(store.redis).mark("old-name", "sw:3:c1") is False
    assert SeenMarks(store.redis).mark("someone-else", "sw:3:c1") is True
    Dispatcher(store).release(name, "bridge-1")
    assert SeenMarks(store.redis).mark("old-name", "sw:3:c2") is True
    assert store.redis.sismember(SeenMarks(store.redis).key(name), "sw:3:c2")
    assert SeenMarks(store.redis).seen("old-name", "sw:3:c2") is True
    assert SeenMarks(store.redis).mark(name, "sw:3:c2") is False


def test_a_committed_delivery_is_never_redelivered(store, dispatcher):
    item = store.send("alice", "bob", "hi")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    send(dispatcher, "bridge-1", delivery)
    assert store.redeliver(now_ms() + 10**9, 0) == []
    assert store.get(item.id).state == "delivered"
    assert dispatcher.reserve("bob", "bridge-1") == []


@pytest.mark.parametrize("evidence", ["", "not a digest"])
def test_a_malformed_payload_is_rejected(store, dispatcher, evidence):
    item = store.send("alice", "bob", "hi")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    dispatcher.receipts.submitting(delivery.id, "bridge-1")
    assert dispatcher.receipts.accept(delivery.id, "bridge-1", evidence).state == "rejected"
    assert store.get(item.id).state == "pending"


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
    dispatcher.receipts.submitting(delivery.id, "bridge-1")
    monkeypatch.setattr(dispatcher.receipts, "_commit", lambda *_: None)
    dispatcher.receipts.accept(delivery.id, "bridge-1", delivery.digest)
    monkeypatch.undo()
    second = store.send("operator", "bob", "comment again", ref="sw:3:c1")
    assert dispatcher.reserve("bob", "bridge-1") == []
    assert store.get(second.id).state == "done"


def test_a_ref_rejected_frees_the_next_item(store, dispatcher):
    first = store.send("operator", "bob", "comment", ref="sw:3:c1")
    second = store.send("operator", "bob", "comment again", ref="sw:3:c1")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    dispatcher.receipts.reject(delivery.id, "bridge-1", "turn refused")
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
    dispatcher.receipts.submitting(delivery.id, "bridge-1")
    store.seats.occupy("eng-1@sw", "carol", 2)
    superseded = dispatcher.receipts.accept(delivery.id, "bridge-1", delivery.digest)
    assert (superseded.state, superseded.reason) == ("superseded", REASSIGNED)
    assert store.get(item.id).state == "pending"
    assert SeenMarks(store.redis).seen("bob", "sw:3:c1") is False
    assert dispatcher.receipts.recover("bob", "bridge-1") == []
    assert [shown.id for shown in claim(store, "carol")] == [item.id]


def test_an_item_closed_while_reserved_is_superseded(store, dispatcher):
    item = store.send("alice", "bob", "hi")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    dispatcher.receipts.submitting(delivery.id, "bridge-1")
    store.close(item.id, "bob", "cancel")
    assert dispatcher.receipts.accept(delivery.id, "bridge-1", delivery.digest).state == "superseded"
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


@pytest.fixture
def watched(monkeypatch):
    from redis.client import Pipeline

    keys = []
    real = Pipeline.watch

    def watch(self, *names):
        keys.extend(names)
        return real(self, *names)

    monkeypatch.setattr(Pipeline, "watch", watch)
    return keys


def journal(store, item_id):
    prefix = store.key("delivery", "")
    found = [Receipts(store).get(key[len(prefix) :]) for key in store.redis.scan_iter(match=prefix + "*")]
    return [delivery for delivery in found if delivery.item == item_id]


def test_own_and_release_watch_the_owner_and_open_deliveries(store, watched):
    dispatcher = Dispatcher(store)
    dispatcher.own("bob", "bridge-1")
    assert set(watched) == {owner_key("bob")}
    watched.clear()
    dispatcher.release("bob", "bridge-1")
    assert set(watched) == {owner_key("bob"), store.key("deliveries", "bob")}


def test_reserve_watches_every_key_it_reads(store, dispatcher, watched):
    first = store.send("operator", "bob", "comment", ref="sw:3:c1")
    [held] = dispatcher.reserve("bob", "bridge-1")
    second = store.send("operator", "bob", "comment again", ref="sw:3:c1")
    watched.clear()
    assert dispatcher.reserve("bob", "bridge-1") == []
    assert {
        owner_key("bob"),
        store.key("item", first.id),
        store.key("reservation", first.id),
        store.key("item", second.id),
        store.key("reservation", second.id),
        SeenMarks.key("bob"),
        store.key("ref-reservation", "bob", "sw:3:c1"),
        store.key("delivery", held.id),
    } <= set(watched)


def test_reserve_and_commit_watch_the_seat_of_a_seat_item(store, watched):
    store.seats.occupy("eng-1@sw", "bob", 1)
    item = store.send("alice", "eng-1@sw", "hi")
    dispatcher = Dispatcher(store)
    dispatcher.own("bob", "bridge-1")
    watched.clear()
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    assert store.seats.key("eng-1@sw") in watched
    dispatcher.receipts.submitting(delivery.id, "bridge-1")
    watched.clear()
    dispatcher.receipts.accept(delivery.id, "bridge-1", delivery.digest)
    assert {store.key("item", item.id), store.seats.key("eng-1@sw"), store.key("pending", "eng-1@sw")} <= set(watched)


def test_a_seen_mark_watches_the_alias_and_the_resolved_owner(store, watched):
    store.redis.set(NameRegistry.key("alias", "old-name"), "bob")
    assert SeenMarks(store.redis).mark("old-name", "sw:3:c1") is True
    assert set(watched) == {NameRegistry.key("alias", "old-name"), owner_key("bob")}


def test_a_seen_mark_expires(store):
    SeenMarks(store.redis).mark("bob", "sw:3:c1")
    assert 0 < store.redis.ttl(SeenMarks.key("bob")) <= 30 * 24 * 3600


def test_digest_is_the_sha256_of_the_payload_with_sorted_keys(store):
    item = store.send("alice", "bob", "hi", ref="sw:3:c1")
    payload = {"address": "bob", "id": item.id, "ref": "sw:3:c1", "sender": "alice", "task": item.task, "text": "hi"}
    assert digest(item) == hashlib.sha256(json.dumps(payload).encode()).hexdigest()


def test_delivery_ids_are_twelve_hex_characters(store, dispatcher):
    store.send("alice", "bob", "hi")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    assert len(delivery.id) == 12
    int(delivery.id, 16)


def test_an_item_without_a_ref_is_free(store, dispatcher):
    with store.redis.pipeline() as pipe:
        assert dispatcher._ref_state(pipe, "bob", "", set()) == "free"


def test_a_superseded_item_journals_its_delivery_and_keeps_the_recipient_waiting(store, dispatcher, monkeypatch):
    from scripts.inbox import dispatch

    store.redis.sadd(SeenMarks.key("bob"), "sw:3:c1")
    shown = store.send("operator", "bob", "comment", ref="sw:3:c1")
    store.send("alice", "bob", "hi")
    monkeypatch.setattr(dispatch, "now_ms", lambda: shown.updated_at + 5000)
    dispatcher.reserve("bob", "bridge-1")
    [superseded] = journal(store, shown.id)
    assert (superseded.state, superseded.reason, superseded.owner, superseded.committed) == (
        "superseded",
        SHOWN,
        "bridge-1",
        False,
    )
    assert store.get(shown.id).updated_at == shown.updated_at + 5000
    assert store.redis.sismember(store.key("waiting"), "bob")


def test_an_item_for_another_address_does_not_stop_the_items_after_it(store, dispatcher, monkeypatch):
    moved = store.send("alice", "bob", "hi")
    later = store.send("alice", "bob", "again")
    listed = store.pending_mail("bob")
    store.redirect(moved.id, "swarm", "carol", "moved")
    monkeypatch.setattr(store, "pending_mail", lambda _: listed)
    assert [d.item for d in dispatcher.reserve("bob", "bridge-1")] == [later.id]


def test_a_held_ref_does_not_stop_the_items_after_it(store, dispatcher):
    first = store.send("operator", "bob", "comment", ref="sw:3:c1")
    store.send("operator", "bob", "comment again", ref="sw:3:c1")
    plain = store.send("alice", "bob", "hi")
    assert [d.item for d in dispatcher.reserve("bob", "bridge-1")] == [first.id, plain.id]


def test_reserve_after_release_names_nobody(store, dispatcher):
    dispatcher.release("bob", "bridge-1")
    with pytest.raises(DispatchError) as refused:
        dispatcher.reserve("bob", "bridge-1")
    assert str(refused.value) == "bridge-1 does not deliver for bob; nobody does"


def test_get_of_an_unknown_delivery_names_it(store):
    with pytest.raises(DispatchError) as refused:
        Receipts(store).get("nope")
    assert str(refused.value) == "no delivery nope"


def test_a_commit_stamps_the_item_and_the_journal(store, monkeypatch):
    from scripts.inbox import receipts

    item = store.send("alice", "bob", "hi")
    store.deliver(item.id, "bob")
    store.requeue(item.id, "bob", "turned back")
    dispatcher = Dispatcher(store)
    dispatcher.own("bob", "bridge-1")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    at = item.updated_at + 5000
    monkeypatch.setattr(receipts, "now_ms", lambda: at)
    assert dispatcher.receipts.submitting(delivery.id, "bridge-1").at == at
    committed = dispatcher.receipts.accept(delivery.id, "bridge-1", delivery.digest)
    assert (committed.state, committed.reason, committed.committed, committed.at) == ("accepted", "", True, at)
    assert dispatcher.receipts.get(delivery.id) == committed
    delivered = store.get(item.id)
    assert (delivered.state, delivered.reason, delivered.updated_at) == ("delivered", "", at)
    assert not store.redis.sismember(store.key("waiting"), "bob")


def test_a_rejection_is_stamped_and_never_committed(store, dispatcher, monkeypatch):
    from scripts.inbox import receipts

    item = store.send("alice", "bob", "hi")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    at = item.updated_at + 5000
    monkeypatch.setattr(receipts, "now_ms", lambda: at)
    rejected = dispatcher.receipts.reject(delivery.id, "bridge-1", "turn refused")
    assert (rejected.committed, rejected.at) == (False, at)
    assert dispatcher.receipts.get(delivery.id) == rejected


def test_a_takeover_moves_recovered_deliveries_to_the_new_owner(store, dispatcher):
    store.send("alice", "bob", "hi")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    dispatcher.receipts.submitting(delivery.id, "bridge-1")
    dispatcher.own("bob", "bridge-2", takeover=True)
    [recovered] = dispatcher.receipts.recover("bob", "bridge-2")
    assert (recovered.state, recovered.owner) == ("unknown", "bridge-2")
    assert dispatcher.receipts.get(delivery.id).owner == "bridge-2"


def test_claim_names_the_recipient_when_it_returns_an_item(store, monkeypatch):
    from scripts.inbox import seen
    from scripts.inbox.store import InboxError

    raced = store.send("operator", "bob", "comment", ref="sw:3:c1")

    def contended(self, name, ref):
        raise InboxError("changed")

    monkeypatch.setattr(seen.SeenMarks, "mark", contended)
    assert claim(store, "bob") == []
    assert store.history(raced.id)[-1]["by"] == "bob"
