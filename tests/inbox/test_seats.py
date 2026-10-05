import pytest

import hooks.context.inbox_delivery as delivery
from scripts.inbox.store import InboxError, InboxStore

pytestmark = pytest.mark.xdist_group("fakeredis")

SEAT = "eng-1@rig"


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


def test_an_item_sent_to_a_seat_is_delivered_to_whichever_agent_occupies_it(store):
    store.seats.occupy(SEAT, "rig-eng-1", at=1)
    item = store.send("rig-master-1", SEAT, "take the flaky test")
    assert [i.id for i in store.mailbox("rig-eng-1")] == [item.id]
    assert store.deliver(item.id, "rig-eng-1").state == "delivered"
    assert store.deliver(item.id, "rig-eng-2") is None


def test_a_successor_gets_its_predecessors_pending_items(store):
    store.seats.occupy(SEAT, "rig-eng-1", at=1)
    item = store.send("rig-master-1", SEAT, "rebase onto dev")
    store.seats.occupy(SEAT, "rig-eng-4", at=1)
    assert store.mailbox("rig-eng-1") == []
    assert store.deliver(item.id, "rig-eng-1") is None
    assert [i.id for i in store.mailbox("rig-eng-4")] == [item.id]
    assert store.deliver(item.id, "rig-eng-4").state == "delivered"


def test_the_mailbox_keeps_items_sent_to_the_session_name(store):
    store.seats.occupy(SEAT, "rig-eng-1", at=1)
    direct = store.send("alice", "rig-eng-1", "hi")
    seated = store.send("alice", SEAT, "hi seat")
    assert {i.id for i in store.mailbox("rig-eng-1")} == {direct.id, seated.id}


def test_only_the_occupant_reads_replies_and_closes_a_seat_item(store):
    store.seats.occupy(SEAT, "rig-eng-1", at=1)
    item = store.send("alice", SEAT, "question")
    store.seats.occupy(SEAT, "rig-eng-4", at=1)
    with pytest.raises(InboxError):
        store.read(item.id, "rig-eng-1")
    assert store.read(item.id, "rig-eng-4").state == "read"
    answer = store.reply(item.id, "rig-eng-4", "answer")
    assert (answer.sender, answer.address) == ("rig-eng-4", "alice")
    assert store.get(item.id).state == "done"


def test_a_delivery_racing_a_handover_writes_nothing_and_retries(server, store):
    store.seats.occupy(SEAT, "rig-eng-1", at=1)
    item = store.send("rig-master-1", SEAT, "ship it")
    other = fresh(server)
    reads = []
    watch = store.seats.watch

    def handover_after_read(pipe, address):
        seen = watch(pipe, address)
        reads.append(seen)
        if len(reads) == 1:
            other.seats.occupy(SEAT, "rig-eng-4", at=1)
        return seen

    store.seats.watch = handover_after_read
    assert store.deliver(item.id, "rig-eng-1") is None
    assert [r.occupant for r in reads] == ["rig-eng-1", "rig-eng-4"]
    assert store.get(item.id).state == "pending"
    assert [e["state"] for e in store.history(item.id)] == ["pending"]
    assert other.deliver(item.id, "rig-eng-4").state == "delivered"


def test_hook_delivery_brings_a_seat_item_to_its_occupant(store, monkeypatch):
    monkeypatch.setattr(delivery, "connect", lambda environ=None: store)
    store.seats.occupy(SEAT, "rig-eng-1", at=1)
    item = store.send("rig-master-1", SEAT, "status please")
    out = delivery.pending_context("s1", {"AGENTIHOOKS_AGENT_NAME": "rig-eng-1"})
    assert item.id in out and "status please" in out
    assert store.get(item.id).state == "delivered"


def test_a_wake_note_whose_seat_changes_after_the_watch_writes_nothing(server, store):
    store.seats.occupy(SEAT, "rig-eng-1", at=1)
    item = store.send("rig-master-1", SEAT, "ship it")
    other, watch = fresh(server), store.seats.watch

    def handover_after_watch(pipe, address):
        seen = watch(pipe, address)
        other.seats.occupy(SEAT, "rig-eng-4", at=2)
        return seen

    store.seats.watch = handover_after_watch
    assert store.note(item.id, "woken", "swarm", "prompted", 3, held=(SEAT, 1)) is False
    assert [e for e in store.history(item.id) if "event" in e] == []
