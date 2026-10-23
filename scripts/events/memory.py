import itertools
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

from .bus import EventLog, StateConflict, StateEntry
from .config import BusConfig, QueuePolicy
from .dead import dead_letter
from .envelope import Envelope

Clock = Callable[[], int]


def _monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def subject_matches(pattern: str, subject: str) -> bool:
    want, have = pattern.split("."), subject.split(".")
    for index, token in enumerate(want):
        if token == ">":
            return len(have) > index
        if index == len(have) or token not in ("*", have[index]):
            return False
    return len(want) == len(have)


class _Runtime:
    def __init__(self, clock: Clock):
        self.clock = clock
        self.cond = threading.Condition()
        self.closed = False

    def require_open(self) -> None:
        if self.closed:
            raise RuntimeError("bus is closed")


class _Dedupe:
    def __init__(self, window_ms: int, clock: Clock):
        self._window_ms = window_ms
        self._clock = clock
        self._seen: dict[str, int] = {}

    def first(self, event_id: str) -> bool:
        now = self._clock()
        self._seen = {key: at for key, at in self._seen.items() if now - at < self._window_ms}
        if event_id in self._seen:
            return False
        self._seen[event_id] = now
        return True


@dataclass(eq=False)
class _Message:
    event: Envelope
    attempt: int = 0
    ready_at: int = 0
    settled: bool = False


class MemoryDelivery:
    def __init__(self, pending: "_Pending", message: _Message):
        self._pending = pending
        self._message = message
        self.event = message.event
        self.attempt = message.attempt

    def ack(self) -> None:
        self._pending.settle(self._message, self.attempt)

    def nak(self, delay_ms: int = 0) -> None:
        self._pending.retry(self._message, self.attempt, delay_ms)

    def term(self) -> None:
        self._pending.settle(self._message, self.attempt)


class _Pending:
    def __init__(
        self,
        runtime: _Runtime,
        policy: QueuePolicy,
        source: Callable[[], Iterable[Envelope]],
        dead: EventLog | None,
        identity: str,
    ):
        self._runtime = runtime
        self._policy = policy
        self._source = source
        self._dead = dead
        self._identity = identity
        self._messages: list[_Message] = []

    def add(self, event: Envelope) -> None:
        self._messages.append(_Message(event))

    def take(self, batch: int, timeout_ms: int) -> list[MemoryDelivery]:
        with self._runtime.cond:
            if not self._runtime.closed and self._ready() is None:
                self._runtime.cond.wait(self._limit_ms(timeout_ms) / 1000)
            taken: list[_Message] = []
            while len(taken) < batch and (message := self._ready(taken)) is not None:
                message.attempt += 1
                message.ready_at = self._runtime.clock() + self._wait_ms(message.attempt)
                taken.append(message)
            return [MemoryDelivery(self, message) for message in taken]

    def settle(self, message: _Message, attempt: int) -> None:
        with self._runtime.cond:
            if message.attempt == attempt:
                message.settled = True

    def retry(self, message: _Message, attempt: int, delay_ms: int) -> None:
        with self._runtime.cond:
            if message.attempt == attempt and not message.settled:
                message.ready_at = self._runtime.clock() + delay_ms
                self._runtime.cond.notify_all()

    def _ready(self, taken: list[_Message] = ()) -> _Message | None:
        for event in self._source():
            self.add(event)
        now = self._runtime.clock()
        for message in self._messages:
            if (
                message not in taken
                and not message.settled
                and message.attempt >= self._policy.max_deliver
                and message.ready_at <= now
            ):
                if self._dead is not None:
                    self._dead.publish(
                        f"dead.{message.event.swarm}",
                        dead_letter(message.event, self._identity, message.attempt),
                    )
                message.settled = True
        self._messages = [m for m in self._messages if not m.settled]
        return next((m for m in self._messages if m.ready_at <= now and m not in taken), None)

    def _limit_ms(self, timeout_ms: int) -> int:
        now = self._runtime.clock()
        return max(0, min([timeout_ms, *(m.ready_at - now for m in self._messages)]))

    def _wait_ms(self, attempt: int) -> int:
        backoff = self._policy.backoff_ms
        return backoff[min(attempt, len(backoff)) - 1] if backoff else self._policy.ack_wait_ms


class MemoryConsumer:
    def __init__(self, log: "MemoryEventLog", subject: str, policy: QueuePolicy, durable: str):
        self.subject = subject
        self.policy = policy
        self._log = log
        self._cursor = 0
        self._pending = _Pending(log.runtime, policy, self._pull, log.dead, f"events.{durable}")

    def fetch(self, batch: int, timeout_ms: int) -> list[MemoryDelivery]:
        return self._pending.take(batch, timeout_ms)

    def _pull(self) -> list[Envelope]:
        entries = self._log.entries[self._cursor :]
        self._cursor += len(entries)
        return [event for subject, event in entries if subject_matches(self.subject, subject)]


class MemoryEventLog:
    def __init__(self, runtime: _Runtime, config: BusConfig, dead: EventLog | None):
        self.runtime = runtime
        self.dead = dead
        self.entries: list[tuple[str, Envelope]] = []
        self._config = config
        self._dedupe = _Dedupe(config.dedupe_window_ms, runtime.clock)
        self._consumers: dict[str, MemoryConsumer] = {}

    def publish(self, subject: str, event: Envelope) -> bool:
        with self.runtime.cond:
            self.runtime.require_open()
            if not self._dedupe.first(event.id):
                return False
            self.entries.append((subject, event))
            self.runtime.cond.notify_all()
            return True

    def consumer(self, durable: str, subject: str, policy: QueuePolicy | None = None) -> MemoryConsumer:
        policy = policy or self._config.queue
        with self.runtime.cond:
            existing = self._consumers.get(durable)
            if existing is None:
                existing = self._consumers[durable] = MemoryConsumer(self, subject, policy, durable)
            elif existing.subject != subject:
                raise ValueError(f"durable consumer {durable!r} filters {existing.subject!r}, not {subject!r}")
            elif existing.policy != policy:
                raise ValueError(f"durable consumer {durable!r} already has another policy")
            return existing


class MemoryWorkQueue:
    def __init__(self, runtime: _Runtime, policy: QueuePolicy, dedupe_window_ms: int, dead: EventLog, name: str):
        self.policy = policy
        self._runtime = runtime
        self._dedupe = _Dedupe(dedupe_window_ms, runtime.clock)
        self._pending = _Pending(runtime, policy, lambda: (), dead, f"queue.{name}")

    def offer(self, event: Envelope) -> bool:
        with self._runtime.cond:
            self._runtime.require_open()
            if not self._dedupe.first(event.id):
                return False
            self._pending.add(event)
            self._runtime.cond.notify_all()
            return True

    def take(self, timeout_ms: int) -> MemoryDelivery | None:
        deliveries = self._pending.take(1, timeout_ms)
        return deliveries[0] if deliveries else None


class MemoryStateStore:
    def __init__(self, runtime: _Runtime):
        self._runtime = runtime
        self._history: list[StateEntry] = []
        self._current: dict[str, StateEntry] = {}

    def get(self, key: str) -> StateEntry | None:
        with self._runtime.cond:
            return self._current.get(key)

    def create(self, key: str, value: bytes) -> int:
        with self._runtime.cond:
            self._runtime.require_open()
            if key in self._current:
                raise StateConflict(f"state key {key!r} exists at revision {self._current[key].revision}")
            return self._put(key, value)

    def update(self, key: str, value: bytes, revision: int) -> int:
        with self._runtime.cond:
            self._runtime.require_open()
            entry = self._current.get(key)
            current = None if entry is None else entry.revision
            if current != revision:
                raise StateConflict(f"state key {key!r} is at revision {current}, not {revision}")
            return self._put(key, value)

    def watch(self, key: str, from_revision: int = 0) -> Iterator[StateEntry]:
        for index in itertools.count():
            with self._runtime.cond:
                if index == len(self._history):
                    return
                entry = self._history[index]
            if entry.key == key and entry.revision >= from_revision:
                yield entry

    def _put(self, key: str, value: bytes) -> int:
        entry = StateEntry(key=key, value=value, revision=len(self._history) + 1)
        self._history.append(entry)
        self._current[key] = entry
        return entry.revision


class MemoryBus:
    def __init__(self, config: BusConfig, clock: Clock):
        self._config = config
        self._runtime = _Runtime(clock)
        self.dead = MemoryEventLog(self._runtime, config, None)
        self.events = MemoryEventLog(self._runtime, config, self.dead)
        self._queues: dict[str, MemoryWorkQueue] = {}
        self._buckets: dict[str, MemoryStateStore] = {}

    def queue(self, name: str, policy: QueuePolicy) -> MemoryWorkQueue:
        with self._runtime.cond:
            self._runtime.require_open()
            queue = self._queues.get(name)
            if queue is None:
                queue = self._queues[name] = MemoryWorkQueue(
                    self._runtime, policy, self._config.dedupe_window_ms, self.dead, name
                )
            elif queue.policy != policy:
                raise ValueError(f"work queue {name!r} already has another policy")
            return queue

    def state(self, bucket: str) -> MemoryStateStore:
        with self._runtime.cond:
            self._runtime.require_open()
            return self._buckets.setdefault(bucket, MemoryStateStore(self._runtime))

    def close(self) -> None:
        with self._runtime.cond:
            self._runtime.closed = True
            self._runtime.cond.notify_all()


def open_bus(config: BusConfig, clock: Clock = _monotonic_ms) -> MemoryBus:
    return MemoryBus(config, clock)
