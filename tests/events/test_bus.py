import pytest

from scripts.events.bus import Bus, Consumer, Delivery, EventLog, StateEntry, StateStore, WorkQueue
from scripts.events.config import QueuePolicy
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

    def consumer(self, durable: str, subject: str, policy: QueuePolicy | None = None) -> Consumer:
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
    dead = _EventLog()

    def for_swarm(self, swarm):
        return self

    def queue(self, name: str, policy: QueuePolicy) -> WorkQueue:
        return _WorkQueue()

    def state(self, bucket: str) -> StateStore:
        return _StateStore()

    def close(self) -> None: ...


def test_delivery_protocol_requires_ack_nak_term_and_attempt():
    assert isinstance(_Delivery(), Delivery)
    assert not isinstance(_NoNak(), Delivery)


def test_event_log_protocol_requires_publish_and_durable_consumer():
    assert isinstance(_EventLog(), EventLog)
    assert isinstance(_Consumer(), Consumer)
    assert not isinstance(_WorkQueue(), EventLog)


def test_work_queue_protocol_requires_offer_and_take():
    assert isinstance(_WorkQueue(), WorkQueue)
    assert not isinstance(_Consumer(), WorkQueue)


def test_state_store_protocol_requires_get_create_update_and_watch():
    assert isinstance(_StateStore(), StateStore)
    assert not isinstance(_NoWatch(), StateStore)


def test_bus_protocol_requires_events_queue_state_and_close():
    assert isinstance(_Bus(), Bus)
    assert not isinstance(_EventLog(), Bus)


def test_state_entry_is_a_frozen_key_value_revision_record():
    entry = StateEntry(key="task.t1", value=b"{}", revision=3)
    assert (entry.key, entry.value, entry.revision) == ("task.t1", b"{}", 3)
    with pytest.raises(AttributeError):
        entry.revision = 4
