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


@pytest.mark.parametrize("state", ["read", "confirmed", "done", "blocked", "handed_off", "cancelled"])
def test_durable_receipt_accepts_received_or_explicitly_settled_work(state):
    bus = memory.open_bus(BusConfig())
    store = Items()
    store.add("work", state)
    store.items["work"] = replace(store.get("work"), delivery_session="receiver")
    feed = SeatFeed(lambda: bus, store, "bob", "receiver", seat=lambda: SEAT)
    SeatInbox(bus, SLUG).send(SEAT, Message("work", "alice", "hello"))
    assert feed.wait(0)
    assert feed.settle() == ["work"]
    assert feed.held == {}


def test_missing_redis_item_does_not_acknowledge_its_durable_receipt():
    bus = memory.open_bus(BusConfig())
    feed = SeatFeed(lambda: bus, Items(), "bob", "receiver", seat=lambda: SEAT)
    SeatInbox(bus, SLUG).send(SEAT, Message("missing", "alice", "hello"))
    assert feed.wait(0)
    assert feed.settle() == []
    assert set(feed.held) == {"missing"}


def test_rejected_confirmation_keeps_the_receipt_for_retry(monkeypatch):
    from scripts.inbox.bus import SeatConsumer

    bus = memory.open_bus(BusConfig())
    store = Items()
    store.add("work", "confirmed")
    store.items["work"] = replace(store.get("work"), delivery_session="receiver")
    feed = SeatFeed(lambda: bus, store, "bob", "receiver", seat=lambda: SEAT)
    SeatInbox(bus, SLUG).send(SEAT, Message("work", "alice", "hello"))
    assert feed.wait(0)
    confirm = SeatConsumer.confirm
    monkeypatch.setattr(SeatConsumer, "confirm", lambda consumer, receipt: False)
    assert feed.settle() == []
    assert set(feed.held) == {"work"}
    monkeypatch.setattr(SeatConsumer, "confirm", confirm)
    assert feed.settle() == ["work"]


def test_successor_receives_the_same_id_and_predecessor_cannot_acknowledge_it():
    clock = [0]
    bus = memory.open_bus(BusConfig(), clock=lambda: clock[0])
    store = Items()
    store.add("work", "delivered")
    store.items["work"] = replace(store.get("work"), delivery_session="old-session")
    old = SeatFeed(lambda: bus, store, "bob", "old-session", seat=lambda: SEAT)
    SeatInbox(bus, SLUG).send(SEAT, Message("work", "alice", "hello"))
    assert old.wait(0)
    clock[0] += 60_000
    new = SeatFeed(lambda: bus, store, "bob", "new-session", seat=lambda: SEAT)
    assert new.wait(0)
    assert old.held["work"].event.id == new.held["work"].event.id == "work"
    store.items["work"] = replace(store.get("work"), state="confirmed", delivery_session="new-session")
    assert old.settle() == []
    assert new.settle() == ["work"]


def test_codex_unconfirmed_retry_is_not_closed_by_reference_dedup(recovery_deadline):
    from scripts.inbox.seen import SeenMarks

    rig = BusRig()
    rig.pump.start()
    rig.pump.step()
    item = rig.store.send("alice", ME, "retry this", ref="work-ref")
    SeatInbox(rig.bus, SLUG).send(SEAT, Message(item.id, "alice", "retry this", ref="work-ref"))
    rig.pump.step()
    assert rig.store.get(item.id).state == "delivered"
    assert SeenMarks(rig.store.redis).seen(ME, item.ref)
    rig.store.requeue(item.id, ME, "receiving session changed")
    retried = rig.pump.dispatcher.reserve(ME, rig.pump.binding.owner)
    assert [delivery.item for delivery in retried] == [item.id]
    assert rig.store.get(item.id).state == "pending"
