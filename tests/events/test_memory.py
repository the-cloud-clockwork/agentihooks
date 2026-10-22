import threading

import pytest

from scripts.events import factory, memory
from scripts.events.bus import Bus, Consumer, Delivery, EventLog, StateConflict, StateEntry, StateStore, WorkQueue
from scripts.events.config import BusConfig, QueuePolicy
from scripts.events.envelope import Envelope

pytestmark = pytest.mark.unit


class Clock:
    def __init__(self):
        self.now = 1_000

    def __call__(self) -> int:
        return self.now


def event(event_id: str = "e1", key: str = "t1") -> Envelope:
    return Envelope(
        id=event_id, type="task.claimed", swarm="s", entity="task", key=key, version=1, time=0, author="a", data={}
    )


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def bus(clock):
    return memory.open_bus(BusConfig(dedupe_window_ms=100), clock=clock)


def test_factory_opens_the_memory_backend_by_default():
    opened = factory.open_bus(BusConfig())
    assert isinstance(opened, memory.MemoryBus)
    assert isinstance(opened, Bus)


def test_every_part_satisfies_its_bus_protocol(bus):
    assert isinstance(bus.events, EventLog)
    assert isinstance(bus.events.consumer("d", "swarm.>"), Consumer)
    queue = bus.queue("work", QueuePolicy())
    assert isinstance(queue, WorkQueue)
    queue.offer(event())
    assert isinstance(queue.take(0), Delivery)
    assert isinstance(bus.state("tasks"), StateStore)


def test_open_bus_defaults_to_the_monotonic_millisecond_clock(monkeypatch):
    monkeypatch.setattr(memory.time, "monotonic_ns", lambda: 7_000_000_000)
    queue = memory.open_bus(BusConfig()).queue("work", QueuePolicy(ack_wait_ms=1))
    queue.offer(event())
    queue.take(0)
    monkeypatch.setattr(memory.time, "monotonic_ns", lambda: 7_000_999_999)
    assert queue.take(0) is None
    monkeypatch.setattr(memory.time, "monotonic_ns", lambda: 7_001_000_000)
    assert queue.take(0).attempt == 2


@pytest.mark.parametrize(
    ("pattern", "subject", "matches"),
    [
        ("swarm.s.task", "swarm.s.task", True),
        ("swarm.s.task", "swarm.s.phase", False),
        ("swarm.*.task", "swarm.s.task", True),
        ("swarm.*", "swarm.s.task", False),
        ("swarm.s.task", "swarm.s", False),
        ("swarm.s", "swarm.s.task", False),
        ("swarm.>", "swarm.s.task", True),
        ("swarm.s.>", "swarm.s", False),
        ("swarm.s.>", "swarm.t.task", False),
    ],
)
def test_subject_matches_follows_nats_wildcards(pattern, subject, matches):
    assert memory.subject_matches(pattern, subject) is matches


def test_publish_dedupes_an_event_id_inside_the_window(bus, clock):
    assert bus.events.publish("swarm.s.task", event()) is True
    clock.now += 99
    assert bus.events.publish("swarm.s.task", event()) is False
    clock.now += 101
    assert bus.events.publish("swarm.s.task", event()) is True
    assert [d.event.id for d in bus.events.consumer("d", "swarm.>").fetch(10, 0)] == ["e1", "e1"]


def test_publish_window_restarts_from_the_first_accepted_copy(bus, clock):
    bus.events.publish("swarm.s.task", event())
    clock.now += 100
    assert bus.events.publish("swarm.s.task", event()) is True


def test_consumer_fetches_only_matching_subjects_in_order(bus):
    bus.events.publish("swarm.s.task", event("e1"))
    bus.events.publish("swarm.s.phase", event("e2"))
    bus.events.publish("swarm.s.task", event("e3"))
    deliveries = bus.events.consumer("d", "swarm.s.task").fetch(10, 0)
    assert [(d.event.id, d.attempt) for d in deliveries] == [("e1", 1), ("e3", 1)]


def test_consumer_fetch_respects_the_batch_size(bus):
    for name in ("e1", "e2", "e3"):
        bus.events.publish("swarm.s.task", event(name))
    consumer = bus.events.consumer("d", "swarm.>")
    assert [d.event.id for d in consumer.fetch(2, 0)] == ["e1", "e2"]
    assert [d.event.id for d in consumer.fetch(2, 0)] == ["e3"]
    assert consumer.fetch(2, 0) == []


def test_durable_consumer_keeps_its_cursor_by_name(bus):
    bus.events.publish("swarm.s.task", event("e1"))
    first = bus.events.consumer("d", "swarm.>")
    first.fetch(1, 0)[0].ack()
    bus.events.publish("swarm.s.task", event("e2"))
    assert bus.events.consumer("d", "swarm.>") is first
    assert [d.event.id for d in bus.events.consumer("d", "swarm.>").fetch(5, 0)] == ["e2"]
    assert [d.event.id for d in bus.events.consumer("other", "swarm.>").fetch(5, 0)] == ["e1", "e2"]


def test_durable_consumer_refuses_a_different_subject(bus):
    bus.events.consumer("d", "swarm.>")
    with pytest.raises(ValueError) as raised:
        bus.events.consumer("d", "swarm.s.task")
    assert str(raised.value) == "durable consumer 'd' filters 'swarm.>', not 'swarm.s.task'"


def test_consumer_redelivers_after_ack_wait_until_acked(clock):
    bus = memory.open_bus(BusConfig(queue=QueuePolicy(ack_wait_ms=50)), clock=clock)
    bus.events.publish("swarm.s.task", event())
    consumer = bus.events.consumer("d", "swarm.>")
    consumer.fetch(1, 0)
    clock.now += 49
    assert consumer.fetch(1, 0) == []
    clock.now += 1
    again = consumer.fetch(1, 0)
    assert [d.attempt for d in again] == [2]
    again[0].ack()
    clock.now += 50
    assert consumer.fetch(1, 0) == []


def test_queue_take_hands_an_event_to_one_taker(bus):
    queue = bus.queue("work", QueuePolicy())
    assert queue.offer(event()) is True
    delivery = queue.take(0)
    assert (delivery.event, delivery.attempt) == (event(), 1)
    assert queue.take(0) is None


def test_queue_offer_dedupes_inside_the_window(bus, clock):
    queue = bus.queue("work", QueuePolicy())
    queue.offer(event())
    assert queue.offer(event()) is False
    clock.now += 100
    assert queue.offer(event()) is True


def test_queue_is_shared_by_name_and_refuses_another_policy(bus):
    queue = bus.queue("work", QueuePolicy())
    assert bus.queue("work", QueuePolicy()) is queue
    assert bus.queue("other", QueuePolicy()) is not queue
    with pytest.raises(ValueError) as raised:
        bus.queue("work", QueuePolicy(max_deliver=2))
    assert str(raised.value) == "work queue 'work' already has another policy"


def test_ack_and_term_settle_a_delivery(bus, clock):
    queue = bus.queue("work", QueuePolicy(ack_wait_ms=10))
    queue.offer(event("e1"))
    queue.offer(event("e2"))
    queue.take(0).ack()
    queue.take(0).term()
    clock.now += 10
    assert queue.take(0) is None


def test_nak_redelivers_after_its_delay(bus, clock):
    queue = bus.queue("work", QueuePolicy())
    queue.offer(event())
    queue.take(0).nak(20)
    clock.now += 19
    assert queue.take(0) is None
    clock.now += 1
    assert queue.take(0).attempt == 2


def test_nak_without_delay_redelivers_at_once(bus):
    queue = bus.queue("work", QueuePolicy())
    queue.offer(event())
    queue.take(0).nak()
    assert queue.take(0).attempt == 2


def test_a_zero_backoff_step_delivers_an_event_once_per_fetch(clock):
    bus = memory.open_bus(BusConfig(queue=QueuePolicy(max_deliver=3, backoff_ms=(0,))), clock=clock)
    bus.events.publish("swarm.s.task", event())
    consumer = bus.events.consumer("d", "swarm.>")
    assert [d.attempt for d in consumer.fetch(10, 0)] == [1]
    assert [d.attempt for d in consumer.fetch(10, 0)] == [2]


def waits(monkeypatch):
    seen = []

    def wait(self, timeout=None):
        seen.append(timeout)
        return False

    monkeypatch.setattr(threading.Condition, "wait", wait)
    return seen


@pytest.fixture
def wakes(monkeypatch):
    seen = []
    monkeypatch.setattr(threading.Condition, "notify_all", lambda self: seen.append(self))
    return seen


def test_take_waits_no_longer_than_the_next_redelivery(bus, clock, waits):
    queue = bus.queue("work", QueuePolicy(ack_wait_ms=50))
    assert queue.take(60_000) is None
    queue.offer(event())
    queue.take(60_000)
    clock.now += 10
    queue.take(60_000)
    queue.take(30)
    assert waits == [60.0, 0.04, 0.03]


def test_take_skips_the_wait_once_closed(bus, waits):
    queue = bus.queue("work", QueuePolicy())
    bus.close()
    assert queue.take(60_000) is None
    assert waits == []


def test_every_change_a_taker_waits_on_wakes_it(bus, wakes):
    queue = bus.queue("work", QueuePolicy())
    bus.events.publish("swarm.s.task", event())
    bus.events.publish("swarm.s.task", event())
    queue.offer(event())
    queue.offer(event())
    delivery = queue.take(0)
    delivery.nak()
    delivery.ack()
    delivery.nak()
    bus.close()
    assert len(wakes) == 4


def test_expiry_follows_the_backoff_then_repeats_its_last_step(bus, clock):
    queue = bus.queue("work", QueuePolicy(max_deliver=4, ack_wait_ms=1_000, backoff_ms=(10, 30)))
    queue.offer(event())
    queue.take(0)
    clock.now += 9
    assert queue.take(0) is None
    clock.now += 1
    assert queue.take(0).attempt == 2
    clock.now += 29
    assert queue.take(0) is None
    clock.now += 1
    assert queue.take(0).attempt == 3
    clock.now += 30
    assert queue.take(0).attempt == 4


def test_max_deliver_drops_the_event_after_its_last_wait(bus, clock):
    queue = bus.queue("work", QueuePolicy(max_deliver=2, ack_wait_ms=10))
    queue.offer(event())
    queue.take(0)
    clock.now += 10
    last = queue.take(0)
    assert last.attempt == 2
    last.nak()
    assert queue.take(0) is None
    clock.now += 10
    assert queue.take(0) is None


def test_a_stale_delivery_cannot_settle_its_redelivery(bus, clock):
    queue = bus.queue("work", QueuePolicy(ack_wait_ms=10))
    queue.offer(event())
    stale = queue.take(0)
    clock.now += 10
    current = queue.take(0)
    stale.ack()
    stale.nak()
    assert queue.take(0) is None
    current.nak()
    assert queue.take(0).attempt == 3


def test_settled_delivery_ignores_a_later_nak(bus):
    queue = bus.queue("work", QueuePolicy())
    queue.offer(event())
    delivery = queue.take(0)
    delivery.ack()
    delivery.nak()
    assert queue.take(0) is None


def test_take_waits_for_an_offer(bus):
    queue = bus.queue("work", QueuePolicy())
    taken = []
    taker = threading.Thread(target=lambda: taken.append(queue.take(5_000)), daemon=True)
    taker.start()
    queue.offer(event())
    taker.join(10)
    assert [d.event.id for d in taken] == ["e1"]


def test_fetch_waits_for_a_publish(bus):
    consumer = bus.events.consumer("d", "swarm.>")
    fetched = []
    fetcher = threading.Thread(target=lambda: fetched.append(consumer.fetch(1, 5_000)), daemon=True)
    fetcher.start()
    bus.events.publish("swarm.s.task", event())
    fetcher.join(10)
    assert [[d.event.id for d in batch] for batch in fetched] == [["e1"]]


def test_close_wakes_a_blocked_taker(bus):
    queue = bus.queue("work", QueuePolicy())
    taken = []
    taker = threading.Thread(target=lambda: taken.append(queue.take(5_000)), daemon=True)
    taker.start()
    bus.close()
    taker.join(10)
    assert taken == [None]


def test_close_refuses_further_writes(bus):
    queue = bus.queue("work", QueuePolicy())
    store = bus.state("tasks")
    bus.close()
    calls = [
        lambda: bus.events.publish("swarm.s.task", event()),
        lambda: queue.offer(event()),
        lambda: bus.queue("work", QueuePolicy()),
        lambda: bus.state("tasks"),
        lambda: store.create("k", b"v"),
        lambda: store.update("k", b"v", 1),
    ]
    for call in calls:
        with pytest.raises(RuntimeError) as raised:
            call()
        assert str(raised.value) == "bus is closed"


def test_state_create_then_compare_and_set_update(bus):
    store = bus.state("tasks")
    assert store.get("t1") is None
    assert store.create("t1", b"a") == 1
    assert store.update("t1", b"b", 1) == 2
    assert store.get("t1") == StateEntry(key="t1", value=b"b", revision=2)


def test_state_revisions_count_across_the_bucket(bus):
    store = bus.state("tasks")
    store.create("t1", b"a")
    assert store.create("t2", b"a") == 2
    assert bus.state("tasks") is store
    assert bus.state("phases").create("t1", b"a") == 1


def test_state_create_refuses_an_existing_key(bus):
    store = bus.state("tasks")
    store.create("t1", b"a")
    with pytest.raises(StateConflict) as raised:
        store.create("t1", b"b")
    assert str(raised.value) == "state key 't1' exists at revision 1"


def test_state_update_refuses_a_stale_revision(bus):
    store = bus.state("tasks")
    store.create("t1", b"a")
    store.update("t1", b"b", 1)
    with pytest.raises(StateConflict) as raised:
        store.update("t1", b"c", 1)
    assert str(raised.value) == "state key 't1' is at revision 2, not 1"
    assert store.get("t1").value == b"b"


def test_state_update_refuses_a_missing_key(bus):
    with pytest.raises(StateConflict) as raised:
        bus.state("tasks").update("t1", b"a", 1)
    assert str(raised.value) == "state key 't1' is at revision None, not 1"


def test_state_watch_yields_a_keys_entries_from_a_revision(bus):
    store = bus.state("tasks")
    store.create("t1", b"a")
    store.create("t2", b"x")
    store.update("t1", b"b", 1)
    store.update("t1", b"c", 3)
    assert [e.value for e in store.watch("t1")] == [b"a", b"b", b"c"]
    assert [e.revision for e in store.watch("t1", from_revision=3)] == [3, 4]


def test_state_watch_sees_entries_written_while_it_iterates(bus):
    store = bus.state("tasks")
    store.create("t1", b"a")
    watch = store.watch("t1")
    assert next(watch).value == b"a"
    store.update("t1", b"b", 1)
    assert [e.value for e in watch] == [b"b"]
