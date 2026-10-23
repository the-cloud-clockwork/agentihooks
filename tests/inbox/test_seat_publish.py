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
