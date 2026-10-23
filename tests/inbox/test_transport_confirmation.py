from dataclasses import replace

import pytest

from scripts.events import memory
from scripts.events.config import BusConfig
from scripts.inbox.bus import Message, SeatFeed, SeatInbox
from tests.inbox.test_codex_pump import ME, SLUG, THREAD
from tests.inbox.test_codex_pump_bus import SEAT, BusRig
from tests.inbox.test_seat_feed import Items

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("state", ["pending", "delivered"])
def test_durable_receipt_waits_for_session_confirmation(state):
    bus = memory.open_bus(BusConfig())
    store = Items()
    store.add("work", state)
    store.items["work"] = replace(store.get("work"), delivery_session="receiver")
    feed = SeatFeed(lambda: bus, store, "bob", "receiver", seat=lambda: SEAT)
    SeatInbox(bus, SLUG).send(SEAT, Message("work", "alice", "hello"))
    assert feed.wait(0)
    assert feed.settle() == []
    assert set(feed.held) == {"work"}
    store.items["work"] = replace(store.get("work"), state="confirmed")
    assert feed.settle() == ["work"]
    assert feed.held == {}


@pytest.mark.parametrize("session", ["", "predecessor"])
def test_durable_receipt_does_not_accept_another_sessions_confirmation(session):
    bus = memory.open_bus(BusConfig())
    store = Items()
    store.add("work", "confirmed")
    store.items["work"] = replace(store.get("work"), delivery_session=session)
    feed = SeatFeed(lambda: bus, store, "bob", "receiver", seat=lambda: SEAT)
    SeatInbox(bus, SLUG).send(SEAT, Message("work", "alice", "hello"))
    assert feed.wait(0)
    assert feed.settle() == []
    assert set(feed.held) == {"work"}


def test_codex_acceptance_retains_the_bus_receipt_until_its_thread_confirms(recovery_deadline):
    rig = BusRig()
    rig.store.session_id = THREAD
    rig.pump.feed = SeatFeed(lambda: rig.bus, rig.store, ME, THREAD, seat=lambda: SEAT)
    rig.pump.start()
    rig.pump.step()
    item = rig.store.send("alice", ME, "confirm this")
    SeatInbox(rig.bus, SLUG).send(SEAT, Message(item.id, "alice", "confirm this"))
    rig.pump.step()
    delivered = rig.store.get(item.id)
    assert delivered.state == "delivered"
    assert delivered.delivery_session == THREAD
    assert set(rig.pump.feed.held) == {item.id}
    assert rig.store.redis.zscore(rig.store.key("delivered"), item.id) is not None
    assert rig.store.confirm_shown(ME, delivered.updated_at + 1) == [rig.store.get(item.id)]
    assert rig.store.get(item.id).state == "confirmed"
    assert rig.pump.feed.settle() == [item.id]
    rig.bus_clock[0] += 60_000
    assert SeatInbox(rig.bus, SLUG).consumer(SEAT, "successor").fetch(5, 0) == []
