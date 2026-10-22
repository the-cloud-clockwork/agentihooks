import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import islice

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
    with nats_server.running(tmp_path / "jetstream") as url:
        bus = nats.open_bus(BusConfig(backend="nats", url=url, dedupe_window_ms=1_500, queue=policy))
        try:
            yield Harness(bus, policy, 1_500, 300, lambda ms: time.sleep(ms / 1000), 50)
        finally:
            bus.close()


BACKENDS = [pytest.param(_memory, id="memory"), pytest.param(_nats, id="nats")]


def _event_log(harness: Harness) -> Channel:
    consumer = harness.bus.events.consumer("contract", "swarm.>")
    return Channel(
        lambda envelope: harness.bus.events.publish("swarm.s.task", envelope),
        lambda: consumer.fetch(1, harness.poll_ms),
    )


def _work_queue(harness: Harness) -> Channel:
    queue = harness.bus.queue("contract", harness.policy)

    def take() -> list[Delivery]:
        delivery = queue.take(harness.poll_ms)
        return [] if delivery is None else [delivery]

    return Channel(queue.offer, take)


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
