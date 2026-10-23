from collections.abc import Callable
from time import monotonic

from .bus import Consumer, Delivery, EventLog, WorkQueue
from .config import QueuePolicy
from .envelope import Envelope
from .subjects import PREFIX, subject

LANES = ("eng", "ci", "plan")


def require_owner(swarm: str, event: Envelope) -> None:
    if event.swarm != swarm:
        raise ValueError(f"event swarm {event.swarm!r} does not match bus swarm {swarm!r}")


def queue_lane(swarm: str, name: str) -> str:
    lane = name.removeprefix(f"{PREFIX}.{swarm}.ready.")
    if lane not in LANES:
        raise ValueError(f"queue {name!r} does not belong to swarm {swarm!r}")
    return lane


def scoped_subject(swarm: str, kind: str, logical: str) -> str:
    prefix = subject(swarm, kind)
    if kind == "dead":
        if logical == f"dead.{swarm}":
            return f"{prefix}.event"
        logical = logical.removeprefix(f"dead.{swarm}.")
        if logical == "dead.>":
            logical = ">"
    elif logical in (f"{PREFIX}.>", f"{PREFIX}.*.>"):
        logical = ">"
    elif logical.startswith(f"{PREFIX}.*."):
        logical = logical.removeprefix(f"{PREFIX}.*.")
    elif logical.startswith(f"{PREFIX}.{swarm}."):
        logical = logical.removeprefix(f"{PREFIX}.{swarm}.")
    elif logical.startswith(f"{PREFIX}."):
        raise ValueError(f"subject {logical!r} does not belong to swarm {swarm!r}")
    return f"{prefix}.{logical}"


class ScopedLog:
    def __init__(self, log: EventLog, swarm: str, kind: str):
        self._log, self._swarm, self._kind = log, swarm, kind

    def publish(self, subject: str, event: Envelope) -> bool:
        require_owner(self._swarm, event)
        return self._log.publish(scoped_subject(self._swarm, self._kind, subject), event)

    def consumer(self, durable: str, subject: str, policy: QueuePolicy | None = None) -> Consumer:
        return self._log.consumer(durable, scoped_subject(self._swarm, self._kind, subject), policy)


class ScopedQueue:
    def __init__(self, queue: WorkQueue, swarm: str, policy: QueuePolicy):
        self._queue, self._swarm, self.policy = queue, swarm, policy

    def offer(self, event: Envelope) -> bool:
        require_owner(self._swarm, event)
        return self._queue.offer(event)

    def take(self, timeout_ms: int) -> Delivery | None:
        return self._queue.take(timeout_ms)


class MergedConsumer:
    def __init__(self, logs: Callable[[], list[EventLog]], durable: str, logical: str, policy: QueuePolicy | None):
        self._logs, self._durable, self._logical, self._policy = logs, durable, logical, policy
        self._consumers: dict[int, Consumer] = {}
        self._cursor = 0

    def fetch(self, batch: int, timeout_ms: int) -> list[Delivery]:
        logs = self._logs()
        deliveries = []
        deadline = monotonic() + timeout_ms / 1000
        for _ in logs:
            remaining = deadline - monotonic()
            if remaining <= 0 or len(deliveries) >= batch:
                break
            log = logs[self._cursor % len(logs)]
            self._cursor = (self._cursor + 1) % len(logs)
            consumer = self._consumers.get(id(log))
            if consumer is None:
                consumer = self._consumers[id(log)] = log.consumer(self._durable, self._logical, self._policy)
            deliveries.extend(consumer.fetch(batch - len(deliveries), max(1, int(remaining * 1000 / len(logs)))))
        return deliveries


class RoutedLog:
    def __init__(self, owner: Callable[[str], EventLog], logs: Callable[[], list[EventLog]], policy: QueuePolicy):
        self._owner, self._logs = owner, logs
        self._policy = policy
        self._consumers: dict[str, tuple[str, QueuePolicy, Consumer]] = {}

    def publish(self, subject: str, event: Envelope) -> bool:
        return self._owner(event.swarm).publish(subject, event)

    def consumer(self, durable: str, subject: str, policy: QueuePolicy | None = None) -> Consumer:
        policy = policy or self._policy
        existing = self._consumers.get(durable)
        if existing is not None:
            if existing[:2] != (subject, policy):
                raise ValueError(f"durable consumer {durable!r} already has another filter or policy")
            return existing[2]
        consumer = MergedConsumer(self._logs, durable, subject, policy)
        self._consumers[durable] = (subject, policy, consumer)
        return consumer
