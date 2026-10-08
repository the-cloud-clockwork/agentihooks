import pytest

from scripts.inbox.dispatch import REASSIGNED, RELEASED, Dispatcher, DispatchError, digest
from scripts.inbox.seen import SEEN_ON_LEDGER, SeenMarks, claim, first_showing, write_ref
from scripts.inbox.store import InboxStore, now_ms, owner_key

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
    with pytest.raises(DispatchError) as refused:
        dispatcher.reserve("bob", "bridge-1")
    assert str(refused.value) == "bridge-1 does not deliver for bob; bridge-2 does"
    with pytest.raises(DispatchError) as refused:
        dispatcher.recover("bob", "bridge-1")
    assert str(refused.value) == "bridge-1 does not deliver for bob; bridge-2 does"
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
    with pytest.raises(DispatchError) as refused:
        dispatcher.reserve("bob", "bridge-2")
    assert str(refused.value) == "bridge-2 does not deliver for bob; bridge-1 does"


def test_recover_by_a_non_owner_names_nobody_once_released(store):
    with pytest.raises(DispatchError) as refused:
        Dispatcher(store).recover("bob", "bridge-1")
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
    with pytest.raises(DispatchError) as refused:
        dispatcher.accept(delivery.id, "bridge-1", delivery.digest)
    assert str(refused.value) == f"delivery {delivery.id} is accepted"
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
    with pytest.raises(DispatchError) as refused:
        dispatcher.reject(delivery.id, "bridge-1", "again")
    assert str(refused.value) == f"delivery {delivery.id} is rejected"


def test_release_is_refused_while_a_delivery_is_open(store, dispatcher):
    item = store.send("alice", "bob", "hi")
    [delivery] = dispatcher.reserve("bob", "bridge-1")
    dispatcher.submitting(delivery.id, "bridge-1")
    with pytest.raises(DispatchError) as refused:
        dispatcher.release("bob", "bridge-1")
    assert str(refused.value) == "bob still has open deliveries; accept or reject them before releasing"
    assert dispatcher.owner("bob") == "bridge-1"
    assert claim(store, "bob") == []
    assert dispatcher.release("bob", "bridge-2") is False
    dispatcher.reject(delivery.id, "bridge-1", "turn refused")
    assert dispatcher.release("bob", "bridge-1") is True
    assert [shown.id for shown in claim(store, "bob")] == [item.id]


def test_a_superseded_item_closed_meanwhile_still_returns_the_reservations(store, dispatcher, monkeypatch):
    store.redis.sadd(SeenMarks(store.redis).key("bob"), "sw:3:c1")
    shown = store.send("operator", "bob", "comment", ref="sw:3:c1")
    fresh_item = store.send("alice", "bob", "hi")
    real = store.close

    def closed_meanwhile(item_id, *args):
        real(item_id, "bob", "cancel")
        return real(item_id, *args)

    monkeypatch.setattr(store, "close", closed_meanwhile)
    assert [d.item for d in dispatcher.reserve("bob", "bridge-1")] == [fresh_item.id]
    assert store.get(shown.id).state == "cancelled"


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
    dispatcher.submitting(delivery.id, "bridge-1")
    assert dispatcher.accept(delivery.id, "bridge-1", evidence).state == "rejected"
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
