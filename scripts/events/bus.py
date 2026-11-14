from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .config import QueuePolicy
from .envelope import Envelope


class StateConflict(Exception):
    pass


class PayloadTooLarge(ValueError):
    """The bus refused a message larger than its server limit; sending it again can never succeed."""


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

    def consumer(self, durable: str, subject: str, policy: QueuePolicy | None = None) -> Consumer: ...


@runtime_checkable
class WorkQueue(Protocol):
    def offer(self, event: Envelope) -> bool: ...

    def take(self, timeout_ms: int) -> Delivery | None: ...

    def withdraw(self, stale: Callable[[Envelope], bool]) -> int:
        """Removes every offer still on the queue, taken but unsettled or not, that ``stale`` names and returns how
        many it removed."""
        ...


@runtime_checkable
class StateStore(Protocol):
    def get(self, key: str) -> StateEntry | None: ...

    def get_many(self, keys: list[str]) -> list[StateEntry | None]: ...

    def create(self, key: str, value: bytes) -> int: ...

    def update(self, key: str, value: bytes, revision: int) -> int: ...

    def watch(self, key: str, from_revision: int = 0) -> Iterator[StateEntry]: ...


@runtime_checkable
class Bus(Protocol):
    events: EventLog
    dead: EventLog
    stops: EventLog

    def queue(self, name: str, policy: QueuePolicy) -> WorkQueue: ...

    def state(self, bucket: str) -> StateStore: ...

    def for_swarm(self, swarm: str) -> "Bus": ...

    def close(self) -> None: ...
