import pytest
from scripts.events.bus import Bus, Consumer, Delivery, EventLog, StateConflict, StateEntry, StateStore, WorkQueue
from scripts.events.config import QueuePolicy

from scripts.events import bus
from scripts.events.envelope import Envelope

pytestmark = pytest.mark.unit


class _Delivery:
    event = None
    attempt = 1

    def ack(self) -> None: ...

    def nak(self, delay_ms: int = 0) -> None: ...

    def term(self) -> None: ...


class _NoNak:
    event = None
    attempt = 1

    def ack(self) -> None: ...

    def term(self) -> None: ...


class _Consumer:
    def fetch(self, batch: int, timeout_ms: int) -> list[Delivery]:
        return []


class _EventLog:
    def publish(self, subject: str, event: Envelope) -> bool:
        return True

    def consumer(self, durable: str, subject: str) -> Consumer:
        return _Consumer()


class _WorkQueue:
    def offer(self, event: Envelope) -> bool:
        return True

    def take(self, timeout_ms: int) -> Delivery | None:
        return None


class _StateStore:
    def get(self, key: str) -> StateEntry | None:
        return None

    def create(self, key: str, value: bytes) -> int:
        return 1

    def update(self, key: str, value: bytes, revision: int) -> int:
        return revision + 1

    def watch(self, key: str, from_revision: int = 0):
        return iter(())


class _NoWatch:
    def get(self, key: str) -> StateEntry | None:
        return None

    def create(self, key: str, value: bytes) -> int:
        return 1

    def update(self, key: str, value: bytes, revision: int) -> int:
        return revision + 1


class _Bus:
    events = _EventLog()

    def queue(self, name: str, policy: QueuePolicy) -> WorkQueue:
        return _WorkQueue()

    def state(self, bucket: str) -> StateStore:
        return _StateStore()

    def close(self) -> None: ...


def test_delivery_carries_ack_delayed_nak_term_and_attempt():
    assert isinstance(_Delivery(), Delivery)
    assert not isinstance(_NoNak(), Delivery)


def test_event_log_publishes_with_dedupe_and_opens_durable_consumers():
    assert isinstance(_EventLog(), EventLog)
    assert isinstance(_Consumer(), Consumer)
    assert not isinstance(_WorkQueue(), EventLog)


def test_work_queue_offers_and_takes():
    assert isinstance(_WorkQueue(), WorkQueue)
    assert not isinstance(_Consumer(), WorkQueue)


def test_state_store_gets_creates_updates_and_watches_from_a_revision():
    assert isinstance(_StateStore(), StateStore)
    assert not isinstance(_NoWatch(), StateStore)


def test_bus_composes_the_event_log_queues_and_state_stores():
    assert isinstance(_Bus(), Bus)
    assert not isinstance(_EventLog(), Bus)


def test_state_entry_is_a_frozen_key_value_revision_record():
    entry = StateEntry(key="task.t1", value=b"{}", revision=3)
    assert (entry.key, entry.value, entry.revision) == ("task.t1", b"{}", 3)
    with pytest.raises(AttributeError):
        entry.revision = 4


def test_state_conflict_is_the_compare_and_set_failure():
    assert issubclass(StateConflict, Exception)
    assert bus.StateConflict is StateConflict
