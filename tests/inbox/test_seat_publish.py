import pytest

from scripts.events import memory
from scripts.events.config import BusConfig

pytestmark = pytest.mark.unit

SEAT = "eng-1@swarm"


@pytest.fixture
def mail(monkeypatch):
    import fakeredis

    from scripts.inbox import bus
    from scripts.inbox.store import InboxStore

    shared = memory.open_bus(BusConfig())
    monkeypatch.setenv("AGENTIHOOKS_BUS", "nats")
    publisher = bus.SeatPublisher(lambda: shared)
    monkeypatch.setattr(bus, "_publisher", lambda config: publisher)
    yield InboxStore(fakeredis.FakeRedis(decode_responses=True)), shared
    shared.close()


def test_an_item_sent_to_a_seat_is_published_to_its_bus_inbox_with_its_item_id(mail):
    from scripts.inbox.bus import SeatInbox

    store, shared = mail
    item = store.send("alice", SEAT, "work", ref="ledger:1", task="t1")
    [receipt] = SeatInbox(shared, "swarm").consumer(SEAT, "session").fetch(5, 0)
    assert receipt.event.id == item.id
    assert receipt.event.data == {
        "id": item.id,
        "sender": "alice",
        "text": "work",
        "ref": "ledger:1",
        "fyi": False,
        "task": "t1",
    }


def test_an_item_sent_to_an_agent_name_stays_off_the_bus(mail):
    store, shared = mail
    store.send("alice", "engineer@323133-0001", "work")
    assert shared.events.entries == []


def test_direct_swarm_mail_publishes_a_message_keyed_wake_event(mail, monkeypatch):
    store, shared = mail
    monkeypatch.setattr(store.names, "slug_of", lambda name: "swarm")
    first = store.send("alice", "engineer@323133-0001", "first")
    second = store.send("alice", "engineer@323133-0001", "second")
    reader = shared.events.consumer("wake-proof", "swarm.swarm.inbox-wake.requested")
    events = [delivery.event for delivery in reader.fetch(10, 0)]
    assert [event.key for event in events] == [first.id, second.id]
    assert [event.type for event in events] == ["inbox.wake", "inbox.wake"]
    assert [event.data["address"] for event in events] == [first.address, second.address]


def test_direct_publication_without_a_swarm_does_not_open_an_inbox(mail):
    from scripts.inbox import bus

    store, shared = mail
    item = store.send("alice", "unregistered", "request")
    bus.SeatPublisher(lambda: shared).publish(item)
    bus.publish(item)
    assert shared.events.entries == []


def test_seat_publication_does_not_resolve_an_unneeded_direct_swarm(mail, monkeypatch):
    store, shared = mail

    def refuse_lookup(name):
        pytest.fail("a seat already identifies its swarm")

    monkeypatch.setattr(store.names, "slug_of", refuse_lookup)
    item = store.send("alice", SEAT, "request")
    [delivery] = shared.events.consumer("seat-lookup-proof", "swarm.swarm.inbox-wake.requested").fetch(10, 0)
    assert delivery.event.version == 1
    assert delivery.event.key == item.id


def test_direct_wake_publication_keeps_the_initial_generation(mail, monkeypatch):
    store, shared = mail
    monkeypatch.setattr(store.names, "slug_of", lambda name: "swarm")
    item = store.send("alice", "unregistered", "request")
    [delivery] = shared.events.consumer("direct-version-proof", "swarm.swarm.inbox-wake.requested").fetch(10, 0)
    assert delivery.event.version == 1
    assert delivery.event.key == item.id


def test_without_the_nats_bus_nothing_is_published(mail, monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_BUS")
    store, shared = mail
    store.send("alice", SEAT, "work")
    assert shared.events.entries == []


def test_a_failed_publish_keeps_the_redis_send_and_says_so(monkeypatch, capsys):
    import fakeredis

    from scripts.inbox import bus
    from scripts.inbox.store import InboxStore

    attempts = []

    def broken():
        attempts.append(1)
        raise OSError("nats unreachable")

    monkeypatch.setenv("AGENTIHOOKS_BUS", "nats")
    publisher = bus.SeatPublisher(broken)
    monkeypatch.setattr(bus, "_publisher", lambda config: publisher)
    store = InboxStore(fakeredis.FakeRedis(decode_responses=True))
    item = store.send("alice", SEAT, "work")
    assert store.get(item.id).state == "pending"
    assert "nats unreachable" in capsys.readouterr().err
    later = store.send("alice", SEAT, "more")
    assert store.get(later.id).state == "pending"
    assert attempts == [1]
    assert capsys.readouterr().err == ""
