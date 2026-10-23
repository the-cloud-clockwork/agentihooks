from dataclasses import replace

import pytest

from scripts.events import memory
from scripts.events.config import BusConfig
from scripts.inbox import bus
from scripts.inbox.bus import FeedError, Message, SeatFeed, SeatInbox, SeatPublisher
from scripts.inbox.store import InboxError, Item

pytestmark = pytest.mark.unit

SEAT = "eng-1@swarm"
OTHER = "eng-2@swarm"


class Items:
    def __init__(self):
        self.items = {}
        self.session_id = ""

    def add(self, item_id, state="pending"):
        self.items[item_id] = Item(item_id, "alice", "bob", "work", state, 1, 1)

    def get(self, item_id):
        if item_id not in self.items:
            raise InboxError(f"no message {item_id}")
        return self.items[item_id]


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Closing:
    def __init__(self, inner):
        self.inner = inner
        self.closed = 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def close(self):
        self.closed += 1
        self.inner.close()


def mailed(shared, seat, item_id):
    SeatInbox(shared, bus._swarm_of(seat)).send(seat, Message(item_id, "alice", "work"))


@pytest.fixture
def seated():
    import fakeredis

    from scripts.inbox.store import InboxStore

    store = InboxStore(fakeredis.FakeRedis(decode_responses=True))
    store.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    return store


def test_seat_of_names_the_seat_a_receiver_still_holds(seated):
    assert bus.seat_of(seated, "sw-eng-1") == "eng-1@sw"
    assert bus.seat_of(seated, "sw-eng-2") == ""
    seated.seats.record_exit("sw-eng-1", "eng-1@sw", "left")
    assert bus.seat_of(seated, "sw-eng-1") == ""


def test_a_feed_without_a_seat_reader_follows_the_receivers_seat(seated):
    shared = memory.open_bus(BusConfig())
    feed = SeatFeed(lambda: shared, seated, "sw-eng-1", "session-1")
    mailed(shared, "eng-1@sw", "first")
    assert feed.wait(0)
    assert set(feed.held) == {"first"}


def test_a_feed_binds_each_seat_once_and_rebinds_when_its_receiver_changes_seat(monkeypatch):
    shared = memory.open_bus(BusConfig())
    bound = []
    real_consumer = SeatInbox.consumer

    def consumer(inbox, address, session):
        bound.append(address)
        return real_consumer(inbox, address, session)

    monkeypatch.setattr(SeatInbox, "consumer", consumer)
    seat = [SEAT]
    feed = SeatFeed(lambda: shared, Items(), "bob", "session-1", seat=lambda: seat[0])
    assert not feed.wait(0)
    assert not feed.wait(0)
    assert bound == [SEAT]
    seat[0] = OTHER
    mailed(shared, OTHER, "moved")
    assert feed.wait(0)
    assert not feed.wait(0)
    assert bound == [SEAT, OTHER]
    assert set(feed.held) == {"moved"}


def test_settle_confirms_every_item_that_left_pending_and_keeps_the_rest():
    shared = memory.open_bus(BusConfig())
    items = Items()
    items.add("waiting")
    items.add("shown", "delivered")
    feed = SeatFeed(lambda: shared, items, "bob", "session-1", seat=lambda: SEAT)
    for item_id in ("waiting", "shown", "gone"):
        mailed(shared, SEAT, item_id)
    assert feed.wait(0)
    assert feed.settle() == ["shown", "gone"]
    assert set(feed.held) == {"waiting"}


def test_a_store_failure_in_settle_is_a_feed_error_naming_it():
    class Broken(Items):
        def get(self, item_id):
            raise RuntimeError("redis gone")

    shared = memory.open_bus(BusConfig())
    feed = SeatFeed(lambda: shared, Broken(), "bob", "session-1", seat=lambda: SEAT)
    mailed(shared, SEAT, "first")
    assert feed.wait(0)
    with pytest.raises(FeedError, match="redis gone"):
        feed.settle()
    assert feed.held == {}


def test_a_feed_without_a_seat_sleeps_out_its_wait(monkeypatch):
    slept = []
    monkeypatch.setattr(bus.time, "sleep", slept.append)
    feed = SeatFeed(lambda: None, Items(), "bob", "session-1", seat=lambda: "")
    assert not feed.wait(250)
    assert slept == [0.25]


def test_a_lost_feed_reopens_only_once_its_cooldown_has_passed(monkeypatch):
    clock = Clock(0.5)
    monkeypatch.setattr(bus.time, "monotonic", clock)
    opened = []

    def opener():
        opened.append(Closing(memory.open_bus(BusConfig())))
        return opened[-1]

    feed = SeatFeed(opener, Items(), "bob", "session-1", seat=lambda: SEAT)
    assert not feed.wait(0)
    assert len(opened) == 1
    monkeypatch.setattr(bus.SeatConsumer, "fetch", lambda consumer, batch, timeout_ms: 1 / 0)
    with pytest.raises(FeedError):
        feed.wait(0)
    assert opened[0].closed == 1
    monkeypatch.undo()
    monkeypatch.setattr(bus.time, "monotonic", clock)
    monkeypatch.setattr(bus.time, "sleep", lambda seconds: None)
    clock.now = 0.5 + bus.REOPEN_S - 0.01
    assert not feed.wait(0)
    assert len(opened) == 1
    clock.now = 0.5 + bus.REOPEN_S
    assert not feed.wait(0)
    assert len(opened) == 2
    mailed(opened[1].inner, SEAT, "after")
    assert feed.wait(0)
    feed.close()
    assert opened[1].closed == 1


def test_feed_for_builds_a_nats_feed_for_the_receiving_session(monkeypatch):
    configs, seats = [], []
    monkeypatch.setenv("AGENTIHOOKS_BUS", "nats")
    monkeypatch.setattr(bus, "open_bus", lambda config: configs.append(config) or "opened")
    monkeypatch.setattr(bus, "seat_of", lambda store, me: seats.append((store, me)) or SEAT)
    items = Items()
    items.session_id = "claude-session"
    assert bus.feed_for(items, "bob", "execution")._session == "execution"
    feed = bus.feed_for(items, "bob")
    assert feed._session == "claude-session"
    assert feed._store is items
    assert feed._opener() == "opened"
    assert configs[0].backend == "nats"
    assert feed._seat() == SEAT
    assert seats == [(items, "bob")]
    items.session_id = ""
    assert bus.feed_for(items, "bob")._session == "bob"


def test_a_failed_publish_closes_its_bus_and_retries_after_the_cooldown(monkeypatch, capsys):
    class Unwritable:
        closed = 0

        def state(self, bucket):
            raise OSError("bus refused")

        def close(self):
            Unwritable.closed += 1

    clock = Clock(0.5)
    monkeypatch.setattr(bus.time, "monotonic", clock)
    opened = []
    publisher = SeatPublisher(lambda: opened.append(Unwritable()) or opened[-1])
    item = Item("first", "alice", SEAT, "work", "pending", 1, 1)
    publisher.publish(item)
    assert len(opened) == 1
    assert Unwritable.closed == 1
    assert "bus refused" in capsys.readouterr().err
    clock.now = 0.5 + bus.REOPEN_S - 0.01
    publisher.publish(replace(item, id="second"))
    assert len(opened) == 1
    clock.now = 0.5 + bus.REOPEN_S
    publisher.publish(replace(item, id="third"))
    assert len(opened) == 2
