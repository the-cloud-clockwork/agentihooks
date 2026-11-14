"""Execution stop on the events engine: the hub asks the hive spoke that started an execution to reap it, and that
spoke ends it through the normal execution exited event."""

import hashlib
import json
import sys
import time
from dataclasses import replace
from typing import Any, cast

from scripts.events.bus import Bus, Consumer, Delivery
from scripts.events.config import QueuePolicy
from scripts.events.envelope import Envelope
from scripts.events.subjects import subject
from scripts.hive import execution_events
from scripts.hive.stop_names import durable
from scripts.swarm.store import AgentRecord
from scripts.swarm_v2.runtime.base import NamedRuntime

TYPE = "execution.stop"
CLAIMS = "spoke-work"
SENT = "sent"
UNKNOWN = "unknown"
ENDED = "ended"
FOREIGN = "foreign"
STOPPED = "stopped"
FAILED = "failed"
REASON = "stopped"
BATCH = 10
PENDING = "pending"
RETRY_MS = 30_000
POLICY = QueuePolicy(max_deliver=100_000, ack_wait_ms=RETRY_MS)


def stop_subject(swarm: str, hive: str) -> str:
    """The subject one hive reads the stops for its executions in a swarm from."""
    return subject(swarm, "execution", "stop", hive)


def _now() -> int:
    """Wall clock milliseconds for an event time."""
    return time.time_ns() // 1_000_000


def _fields(event: Envelope) -> dict[str, Any]:
    """The event's data with its keys typed."""
    return cast("dict[str, Any]", event.data)


def started(bus: Bus, swarm: str, name: str, generation: int) -> Envelope | None:
    """The started event a hive spoke recorded for the execution, or None when no spoke started it."""
    entry = bus.for_swarm(swarm).state(execution_events.STARTS).get(execution_events.identity(swarm, name, generation))
    if entry is None or json.loads(entry.value) is None:
        return None
    event = Envelope.decode(entry.value)
    return event if _fields(event).get("claim_key") else None


def ended(bus: Bus, event: Envelope) -> bool:
    """True once the execution's exited event is out or its claim left the started state it was launched under."""
    scoped = bus.for_swarm(event.swarm)
    exited = hashlib.sha256(f"exited:{event.id}".encode()).hexdigest()
    if scoped.state(execution_events.STATE).get(exited) is not None:
        return True
    entry = scoped.state(CLAIMS).get(_fields(event)["claim_key"])
    if entry is None:
        return True
    claim = json.loads(entry.value)
    return claim.get("state") != "started" or claim.get("claim_revision") != _fields(event)["claim_revision"]


def request(event: Envelope, reason: str) -> Envelope:
    """The stop event for one started execution."""
    data = {"name": _fields(event)["name"], "hive": _fields(event)["hive"], "reason": reason}
    return replace(event, id=f"stop:{event.id}", type=TYPE, time=_now(), author="hub", data=data, cause=event.id)


def stop(bus: Bus, slug: str, agent: AgentRecord, reason: str) -> str:
    """Publishes the stop for one admitted execution to the hive that started it; answers unknown when no hive started
    it and ended when it already ended, publishing nothing for either, and pending when the same stop is still inside
    the bus dedupe window."""
    event = started(bus, slug, agent.name, agent.generation)
    if event is None or event.key != agent.execution_id:
        return UNKNOWN
    if ended(bus, event):
        return ENDED
    hive = _fields(event)["hive"]
    stops = bus.for_swarm(slug).stops
    stops.consumer(durable(slug, hive), stop_subject(slug, hive), POLICY)
    return SENT if stops.publish(stop_subject(slug, hive), request(event, reason)) else PENDING


def _unprovisioned(exc: Exception) -> bool:
    """True when the bus refuses to bind a stop consumer because the hub has not created it yet."""
    from nats.js.errors import NotFoundError

    return isinstance(exc, NotFoundError)


class Stopper:
    """A hive's reader of the stops addressed to it in one swarm."""

    def __init__(self, bus: Bus, swarm: str, hive: str, runtime: NamedRuntime) -> None:
        self.bus, self.swarm, self.hive, self.runtime = bus.for_swarm(swarm), swarm, hive, runtime
        self._consumer: Consumer | None = None

    def poll(self, timeout_ms: int = 0) -> list[str]:
        """Settles every stop waiting for this hive and answers each outcome; before the hub creates this hive's stop
        consumer, and on a failed read, it answers nothing until the next pass."""
        try:
            if self._consumer is None:
                self._consumer = self.bus.stops.consumer(
                    durable(self.swarm, self.hive), stop_subject(self.swarm, self.hive), POLICY
                )
            deliveries = self._consumer.fetch(BATCH, timeout_ms)
        except Exception as exc:
            if not _unprovisioned(exc):
                self._failed(exc)
            return []
        return [self._settle(delivery) for delivery in deliveries]

    def _failed(self, exc: Exception) -> None:
        """Says on stderr that reading or handling a stop failed."""
        print(f"hive execution stops for swarm {self.swarm} failed ({type(exc).__name__}: {exc})", file=sys.stderr)

    def _settle(self, delivery: Delivery) -> str:
        """Acks a settled stop; a stop whose reap failed or raised is said on stderr and redelivered after a delay, so
        it is kept for as long as the agent lives."""
        try:
            outcome = self.handle(delivery.event)
        except Exception as exc:
            self._failed(exc)
            outcome = FAILED
        if outcome == FAILED:
            name = _fields(delivery.event).get("name")
            print(f"hive execution stop for {name} in swarm {self.swarm} could not reap it; retrying", file=sys.stderr)
            delivery.nak(RETRY_MS)
        else:
            delivery.ack()
        return outcome

    def handle(self, event: Envelope) -> str:
        """Reaps the execution one stop names when this hive owns it, then emits its exit; an execution that already
        ended is still reaped when its agent outlived the exit, and emits nothing."""
        if event.type != TYPE or _fields(event).get("hive") != self.hive:
            return FOREIGN
        found = started(self.bus, event.swarm, _fields(event)["name"], event.version)
        if found is None or found.key != event.key or _fields(found).get("hive") != self.hive:
            return FOREIGN
        gone = ended(self.bus, found)
        if not self._reap(_fields(event)["name"]):
            return FAILED
        if gone:
            return ENDED
        data = {**_fields(found), "reason": REASON, "stop_reason": _fields(event)["reason"]}
        exited = replace(
            found, id=f"exited:{found.id}", type="execution.exited", time=_now(), data=data, cause=found.id
        )
        execution_events.emit(self.bus, exited)
        return STOPPED

    def _reap(self, name: str) -> bool:
        """True once the named agent is gone from this hive, reaping it when it is still there."""
        found = self.runtime.find(name)
        return found.ok and (found.value is None or self.runtime.reap(name).ok)
