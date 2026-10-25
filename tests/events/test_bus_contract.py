import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import islice
from unittest.mock import patch

import pytest

from scripts.events import memory
from scripts.events.bus import Bus, Delivery, StateConflict, StateEntry
from scripts.events.config import BusConfig, QueuePolicy
from scripts.events.envelope import Envelope

from . import nats_server

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class Harness:
    bus: Bus
    policy: QueuePolicy
    dedupe_ms: int
    margin_ms: int
    advance: Callable[[int], None]
    poll_ms: int


@dataclass(frozen=True)
class Channel:
    offer: Callable[[Envelope], bool]
    take: Callable[[], list[Delivery]]
    source: str


@contextmanager
def _memory(tmp_path) -> Iterator[Harness]:
    now = [1_000]

    def advance(ms: int) -> None:
        now[0] += ms

    policy = QueuePolicy(max_deliver=3, ack_wait_ms=50)
    bus = memory.open_bus(BusConfig(dedupe_window_ms=100, queue=policy), clock=lambda: now[0])
    try:
        yield Harness(bus, policy, 100, 1, advance, 0)
    finally:
        bus.close()


@contextmanager
def _nats(tmp_path) -> Iterator[Harness]:
    from scripts.events import nats

    policy = QueuePolicy(max_deliver=3, ack_wait_ms=600)
    dedupe_ms = 1_500
    offered: dict[tuple[str, str, str], float] = {}
    publish = nats._publish

    async def recorded(js, subject: str, stream: str, event: Envelope) -> bool:
        fresh = await publish(js, subject, stream, event)
        if fresh:
            offered[(subject, stream, event.id)] = time.monotonic()
        return fresh

    with nats_server.running(tmp_path / "jetstream") as url, patch.object(nats, "_publish", recorded):
        bus = nats.open_bus(BusConfig(backend="nats", url=url, dedupe_window_ms=dedupe_ms + 100, queue=policy))

        def advance(ms: int) -> None:
            time.sleep(ms / 1000)
            for key in [key for key, at in offered.items() if time.monotonic() - at >= dedupe_ms / 1000]:
                nats_server.wait_forgotten(bus, *key)
                del offered[key]

        try:
            yield Harness(bus, policy, dedupe_ms, 300, advance, 50)
        finally:
            bus.close()


BACKENDS = [pytest.param(_memory, id="memory"), pytest.param(_nats, id="nats")]


def _event_log(harness: Harness) -> Channel:
    consumer = harness.bus.events.consumer("contract", "swarm.>")
    return Channel(
        lambda envelope: harness.bus.events.publish("swarm.s.task", envelope),
        lambda: consumer.fetch(1, harness.poll_ms),
        "events.contract",
    )


def _work_queue(harness: Harness) -> Channel:
    queue = harness.bus.queue("contract", harness.policy)

    def take() -> list[Delivery]:
        delivery = queue.take(harness.poll_ms)
        return [] if delivery is None else [delivery]

    return Channel(queue.offer, take, "queue.contract")


@pytest.fixture(params=BACKENDS)
def harness(request, tmp_path):
    with request.param(tmp_path) as opened:
        yield opened


@pytest.fixture(params=[_event_log, _work_queue], ids=["event-log", "work-queue"])
def channel(request, harness):
    return request.param(harness)


def event(event_id: str = "e1") -> Envelope:
    return Envelope(
        id=event_id, type="task.claimed", swarm="s", entity="task", key="t1", version=1, time=0, author="a", data={}
    )


def seen(deliveries: list[Delivery]) -> list[tuple[str, int]]:
    return [(delivery.event.id, delivery.attempt) for delivery in deliveries]


def test_an_unacked_delivery_comes_back_once_the_ack_wait_passes(channel, harness):
    channel.offer(event())
    assert seen(channel.take()) == [("e1", 1)]
    harness.advance(harness.policy.ack_wait_ms - harness.margin_ms)
    assert channel.take() == []
    harness.advance(harness.margin_ms)
    assert seen(channel.take()) == [("e1", 2)]


def test_an_event_is_never_delivered_past_the_attempt_cap(channel, harness):
    channel.offer(event())
    for attempt in (1, 2, 3):
        assert seen(channel.take()) == [("e1", attempt)]
        harness.advance(harness.policy.ack_wait_ms)
    assert channel.take() == []
    harness.advance(2 * harness.policy.ack_wait_ms)
    assert channel.take() == []


def test_an_exhausted_delivery_is_retained_once_in_the_dead_stream(channel, harness):
    original = event()
    channel.offer(original)
    for attempt in (1, 2, 3):
        delivery = channel.take()[0]
        assert (delivery.event, delivery.attempt) == (original, attempt)
        harness.advance(harness.policy.ack_wait_ms + harness.margin_ms)
    assert channel.take() == []
    dead = harness.bus.dead.consumer("dead-reader", "dead.>")
    letters = dead.fetch(10, harness.poll_ms)
    assert len(letters) == 1
    letter = letters[0].event
    assert letter.type == "dead_lettered"
    assert letter.cause == original.id
    assert (letter.swarm, letter.entity, letter.key, letter.version) == ("s", "task", "t1", 1)
    assert letter.data == {"event": original.to_dict(), "attempts": 3, "source": channel.source}
    assert letter.id != original.id
    letters[0].ack()
    harness.advance(2 * harness.policy.ack_wait_ms)
    assert channel.take() == []
    assert dead.fetch(10, harness.poll_ms) == []


@pytest.mark.parametrize("settlement", ["ack", "term"])
def test_a_settled_final_attempt_is_not_dead_lettered(channel, harness, settlement):
    channel.offer(event())
    for attempt in (1, 2, 3):
        delivery = channel.take()[0]
        assert delivery.attempt == attempt
        if attempt < 3:
            delivery.nak()
    getattr(delivery, settlement)()
    harness.advance(harness.policy.ack_wait_ms + harness.margin_ms)
    assert channel.take() == []
    assert harness.bus.dead.consumer("dead-reader", "dead.>").fetch(1, harness.poll_ms) == []


def test_a_nak_holds_the_event_until_its_delay_passes(channel, harness):
    delay = 2 * harness.policy.ack_wait_ms
    channel.offer(event())
    first = channel.take()
    assert seen(first) == [("e1", 1)]
    first[0].nak(delay)
    harness.advance(delay - harness.margin_ms)
    assert channel.take() == []
    harness.advance(harness.margin_ms)
    redelivered = channel.take()
    assert seen(redelivered) == [("e1", 2)]
    redelivered[0].ack()
    harness.advance(2 * harness.policy.ack_wait_ms)
    assert channel.take() == []


def test_a_consumer_policy_replaces_the_bus_attempt_cap(harness):
    own = QueuePolicy(max_deliver=harness.policy.max_deliver + 2, ack_wait_ms=harness.policy.ack_wait_ms)
    consumer = harness.bus.events.consumer("own", "swarm.>", own)
    original = event()
    harness.bus.events.publish("swarm.s.task", original)
    for attempt in range(1, own.max_deliver + 1):
        deliveries = consumer.fetch(1, harness.poll_ms)
        assert seen(deliveries) == [("e1", attempt)]
        deliveries[0].nak()
    assert consumer.fetch(1, harness.poll_ms) == []
    letters = harness.bus.dead.consumer("dead-reader", "dead.>").fetch(10, harness.poll_ms)
    assert [letter.event.data for letter in letters] == [
        {"event": original.to_dict(), "attempts": own.max_deliver, "source": "events.own"}
    ]


def test_a_durable_consumer_refuses_another_policy_in_the_same_bus(harness):
    own = QueuePolicy(max_deliver=harness.policy.max_deliver + 2, ack_wait_ms=harness.policy.ack_wait_ms)
    harness.bus.events.consumer("own", "swarm.>", own)
    harness.bus.events.consumer("own", "swarm.>", own)
    harness.bus.events.consumer("other", "swarm.>")
    with pytest.raises(ValueError) as raised:
        harness.bus.events.consumer("own", "swarm.>")
    assert str(raised.value) == "durable consumer 'own' already has another policy"


def test_a_repeated_event_id_is_refused_inside_the_dedupe_window(channel, harness):
    assert channel.offer(event("e1")) is True
    harness.advance(harness.dedupe_ms - harness.margin_ms)
    assert channel.offer(event("e1")) is False
    assert channel.offer(event("e2")) is True
    harness.advance(harness.margin_ms)
    assert channel.offer(event("e1")) is True
    delivered = []
    for _ in range(3):
        deliveries = channel.take()
        deliveries[0].ack()
        delivered += seen(deliveries)
    assert delivered == [("e1", 1), ("e2", 1), ("e1", 1)]
    assert channel.take() == []


def test_compare_and_set_refuses_an_existing_key_and_a_stale_revision(harness):
    store = harness.bus.state("contract")
    first = store.create("k", b"v1")
    with pytest.raises(StateConflict):
        store.create("k", b"v2")
    second = store.update("k", b"v2", first)
    with pytest.raises(StateConflict):
        store.update("k", b"v3", first)
    with pytest.raises(StateConflict):
        store.update("missing", b"v1", first)
    assert second > first
    assert store.get("k") == StateEntry("k", b"v2", second)
    assert store.get("missing") is None


def test_watch_yields_a_keys_entries_from_a_revision_in_order(harness):
    store = harness.bus.state("contract")
    first = store.create("k", b"v1")
    other = store.create("other", b"o1")
    second = store.update("k", b"v2", first)
    third = store.update("k", b"v3", second)
    assert other < second
    assert list(islice(store.watch("k", second), 2)) == [
        StateEntry("k", b"v2", second),
        StateEntry("k", b"v3", third),
    ]
    assert list(islice(store.watch("k"), 3)) == [
        StateEntry("k", b"v1", first),
        StateEntry("k", b"v2", second),
        StateEntry("k", b"v3", third),
    ]
