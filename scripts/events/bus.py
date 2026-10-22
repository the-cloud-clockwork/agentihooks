from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .config import QueuePolicy
from .envelope import Envelope


class StateConflict(Exception):
    pass


@dataclass(frozen=True)
class StateEntry:
    key: str
    value: bytes
    revision: int


@runtime_checkable
class Delivery(Protocol):
    event: Envelope
    attempt: int

    def ack(self) -> None: ...

    def nak(self, delay_ms: int = 0) -> None: ...

    def term(self) -> None: ...


@runtime_checkable
class Consumer(Protocol):
    def fetch(self, batch: int, timeout_ms: int) -> list[Delivery]: ...


@runtime_checkable
class EventLog(Protocol):
    def publish(self, subject: str, event: Envelope) -> bool: ...

    def consumer(self, durable: str, subject: str) -> Consumer: ...


@runtime_checkable
class WorkQueue(Protocol):
    def offer(self, event: Envelope) -> bool: ...

    def take(self, timeout_ms: int) -> Delivery | None: ...


@runtime_checkable
class StateStore(Protocol):
    def get(self, key: str) -> StateEntry | None: ...

    def create(self, key: str, value: bytes) -> int: ...

    def update(self, key: str, value: bytes, revision: int) -> int: ...

    def watch(self, key: str, from_revision: int = 0) -> Iterator[StateEntry]: ...


@runtime_checkable
class Bus(Protocol):
    events: EventLog
    dead: EventLog

    def queue(self, name: str, policy: QueuePolicy) -> WorkQueue: ...

    def state(self, bucket: str) -> StateStore: ...

    def close(self) -> None: ...
