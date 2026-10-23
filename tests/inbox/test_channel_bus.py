import threading
import time

import pytest

from scripts.events import memory
from scripts.events.config import BusConfig
from scripts.inbox import channel
from tests.inbox.test_channel_server import FakeStore, session

pytestmark = pytest.mark.xdist_group("mcp-sdk")

SEAT = "eng-1@swarm"


def test_the_channel_pushes_a_seat_item_the_bus_delivers_and_confirms_its_receipt(monkeypatch, mcp_sdk):
    import anyio

    from scripts.inbox.bus import Message, SeatFeed, SeatInbox

    clock = [0]
    bus = memory.open_bus(BusConfig(), clock=lambda: clock[0])
    store = FakeStore()
    feed = SeatFeed(lambda: bus, store, "bob", "session-1", seat=lambda: SEAT)
    entered = threading.Event()
    real_wait = feed.wait

    def wait(timeout_ms):
        entered.set()
        return real_wait(timeout_ms)

    monkeypatch.setattr(feed, "wait", wait)
    monkeypatch.setattr(channel, "SETTLE_S", 0)
    monkeypatch.setattr(channel, "RECHECK_S", 3600)

    async def scenario(send, receive):
        assert await anyio.to_thread.run_sync(lambda: entered.wait(30)), "session never waited on the bus"
        item = store.send("alice", "bob", "hello", notify=False)
        SeatInbox(bus, "swarm").send(SEAT, Message(item.id, "alice", "hello"))
        with anyio.fail_after(10):
            pushed = await receive()
            while feed.held:
                await anyio.sleep(0.01)
        clock[0] += 60_000
        leftover = SeatInbox(bus, "swarm").consumer(SEAT, "later").fetch(5, 0)
        bus.close()
        return item, pushed, leftover

    _, _, (item, pushed, leftover) = session(store, scenario, feed=feed)
    assert pushed == {"jsonrpc": "2.0", **channel.event(store.get(item.id))}
    assert store.get(item.id).state == "delivered"
    assert leftover == []
    assert store.subs == []


def test_a_seat_item_still_pending_keeps_its_bus_receipt_open(mcp_sdk):
    from scripts.inbox.bus import Message, SeatFeed, SeatInbox

    bus = memory.open_bus(BusConfig())
    store = FakeStore()
    item = store.send("alice", "bob", "hello", notify=False)
    feed = SeatFeed(lambda: bus, store, "bob", "session-1", seat=lambda: SEAT)
    SeatInbox(bus, "swarm").send(SEAT, Message(item.id, "alice", "hello"))
    assert feed.wait(0)
    assert feed.settle() == []
    assert set(feed.held) == {item.id}
    store.deliver(item.id, "bob")
    assert feed.settle() == [item.id]
    assert feed.held == {}


def test_a_session_without_a_seat_waits_without_touching_the_bus(mcp_sdk):
    from scripts.inbox.bus import SeatFeed

    opened = []
    feed = SeatFeed(lambda: opened.append(1), FakeStore(), "bob", "session-1", seat=lambda: "")
    assert not feed.wait(0)
    assert feed.settle() == []
    assert opened == []


def test_a_bus_failure_becomes_a_feed_error_and_the_next_wait_reopens_the_bus(monkeypatch, mcp_sdk):
    from scripts.inbox import bus
    from scripts.inbox.bus import FeedError, SeatConsumer, SeatFeed

    buses = []

    def opener():
        buses.append(memory.open_bus(BusConfig()))
        return buses[-1]

    def lost(consumer, batch, timeout_ms):
        raise RuntimeError("bus connection lost")

    feed = SeatFeed(opener, FakeStore(), "bob", "session-1", seat=lambda: SEAT)
    assert not feed.wait(0)
    real_fetch = SeatConsumer.fetch
    monkeypatch.setattr(SeatConsumer, "fetch", lost)
    with pytest.raises(FeedError, match="bus connection lost"):
        feed.wait(0)
    monkeypatch.setattr(SeatConsumer, "fetch", real_fetch)
    assert not feed.wait(0)
    assert feed.settle() == []
    assert len(buses) == 1
    later = time.monotonic() + bus.REOPEN_S + 1
    monkeypatch.setattr(bus.time, "monotonic", lambda: later)
    assert not feed.wait(0)
    assert len(buses) == 2


@pytest.mark.parametrize(("backend", "fed"), [("nats", True), ("memory", False), ("", False)])
def test_only_the_nats_bus_setting_gives_a_receiver_a_seat_feed(monkeypatch, backend, fed, mcp_sdk):
    from scripts.inbox.bus import SeatFeed, feed_for

    monkeypatch.setenv("AGENTIHOOKS_BUS", backend)
    store = FakeStore()
    store.session_id = "session-1"
    assert isinstance(feed_for(store, "bob"), SeatFeed) is fed


def test_the_channel_takes_its_feed_from_the_bus_setting(monkeypatch, mcp_sdk):
    import anyio

    from scripts.inbox.bus import Message, SeatFeed, SeatInbox

    bus = memory.open_bus(BusConfig())
    closed = threading.Event()
    real_close = bus.close

    def close():
        closed.set()
        real_close()

    monkeypatch.setattr(bus, "close", close)
    store = FakeStore()
    feed = SeatFeed(lambda: bus, store, "bob", "session-1", seat=lambda: SEAT)
    asked, waits = [], []
    real_wait = feed.wait

    def wait(timeout_ms):
        waits.append(timeout_ms)
        return real_wait(timeout_ms)

    def feed_for(given, me):
        asked.append((given, me))
        return feed

    monkeypatch.setattr(feed, "wait", wait)
    monkeypatch.setattr(channel, "feed_for", feed_for)
    monkeypatch.setattr(channel, "SETTLE_S", 0)
    monkeypatch.setattr(channel, "RECHECK_S", 0.25)

    async def scenario(send, receive):
        with anyio.fail_after(10):
            while not waits:
                await anyio.sleep(0.01)
        item = store.send("alice", "bob", "hello", notify=False)
        SeatInbox(bus, "swarm").send(SEAT, Message(item.id, "alice", "hello"))
        with anyio.fail_after(10):
            return item, await receive()

    _, _, (item, pushed) = session(store, scenario)
    assert pushed["params"]["meta"]["item_id"] == item.id
    assert asked == [(store, "bob")]
    assert set(waits) == {250}
    assert store.subs == []
    assert closed.is_set()


def test_a_bus_outage_keeps_redis_items_on_the_recheck_cadence(monkeypatch, mcp_sdk):
    import anyio

    from scripts.inbox.bus import SeatConsumer, SeatFeed

    def lost(consumer, batch, timeout_ms):
        raise RuntimeError("bus connection lost")

    monkeypatch.setattr(SeatConsumer, "fetch", lost)
    store = FakeStore()
    feed = SeatFeed(lambda: memory.open_bus(BusConfig()), store, "bob", "session-1", seat=lambda: SEAT)
    monkeypatch.setattr(channel, "SETTLE_S", 0)
    monkeypatch.setattr(channel, "RECHECK_S", 0.2)

    async def scenario(send, receive):
        await anyio.sleep(1.2)
        item = store.send("alice", "bob", "quiet", notify=False)
        with anyio.fail_after(0.6):
            return item, await receive()

    _, _, (item, pushed) = session(store, scenario, feed=feed)
    assert pushed["params"]["meta"]["item_id"] == item.id
