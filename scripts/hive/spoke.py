import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol

from scripts.events.bus import Bus, Delivery, StateConflict
from scripts.events.config import QueuePolicy
from scripts.events.dead import dead_letter
from scripts.events.envelope import Envelope
from scripts.hive import admission, execution_events, headroom, presence
from scripts.swarm import readiness
from scripts.swarm_v2.runtime.base import Runtime, SpawnRequest


class Tasks(Protocol):
    def request(self, event: Envelope) -> SpawnRequest | None:
        """The task carries admitted execution_id, execution_generation and claim_generation."""
        ...

    def reopen(self, event: Envelope) -> None: ...


@dataclass(frozen=True)
class SpokeConfig:
    swarm: Any
    lane: str
    hive: str
    policy: QueuePolicy = QueuePolicy()


def claim_key(event: Envelope) -> str:
    return hashlib.sha256(json.dumps([event.swarm, event.key, event.version]).encode()).hexdigest()


class Spoke:
    def __init__(
        self,
        config: SpokeConfig,
        bus: Bus,
        runtime: Runtime,
        tasks: Tasks,
        measure: Callable[[], dict | None] | None = None,
        clock: Callable[[], int] | None = None,
    ):
        self.config, self.bus, self.runtime, self.tasks = config, bus, runtime, tasks
        self._previous = None
        self.measure = measure or self._measure
        self.clock = clock or presence.now_ms
        self._pending_key = hashlib.sha256(
            json.dumps([config.swarm.slug, config.lane, config.hive]).encode()
        ).hexdigest()

    def _measure(self) -> dict | None:
        sample = headroom.measure(0, time.time_ns() // 1_000_000, previous=self._previous)
        if sample is not None:
            self._previous = sample
        return sample

    def poll(self, timeout_ms: int = 0) -> str:
        try:
            with presence.Heartbeat(self.bus, self.config.hive, self.clock) as heartbeat:
                return self._poll(timeout_ms, heartbeat)
        except Exception:
            return "retry"

    def _poll(self, timeout_ms: int, heartbeat: presence.Heartbeat) -> str:
        recovered = self._recover()
        if recovered in ("busy", "uncertain"):
            return recovered
        heartbeat.check()
        if admission.room(self.measure()).room < 1:
            return recovered or "held"
        try:
            queue = self.bus.queue(readiness.queue_name(self.config.swarm.slug, self.config.lane), self.config.policy)
            delivery = queue.take(timeout_ms)
        except Exception:
            return "retry"
        if delivery is None:
            return recovered or "empty"
        try:
            heartbeat.check()
            return self._apply(delivery, heartbeat)
        except Exception:
            return self._retry(delivery)

    def _retry(self, delivery: Delivery) -> str:
        try:
            if delivery.attempt >= self.config.policy.max_deliver:
                return self._exhaust(delivery)
        except Exception:
            pass
        steps = self.config.policy.backoff_ms or (self.config.policy.ack_wait_ms,)
        delivery.nak(steps[min(delivery.attempt - 1, len(steps) - 1)])
        return "retry"

    def _reserve(self, delivery: Delivery) -> int:
        pending = self.bus.state("spoke-pending")
        entry = pending.get(self._pending_key)
        if entry and json.loads(entry.value):
            raise StateConflict
        value = json.dumps({"event": delivery.event.to_dict(), "attempt": delivery.attempt}).encode()
        return (
            pending.update(self._pending_key, value, entry.revision)
            if entry
            else pending.create(self._pending_key, value)
        )

    def _clear_pending(self, revision: int) -> None:
        self.bus.state("spoke-pending").update(self._pending_key, b"{}", revision)

    def _waiting(self, prior: dict) -> str | None:
        state = prior.get("state")
        if state not in ("claimed", "launching"):
            return None
        if presence.lost(self.bus, prior.get("owner"), self.clock()):
            return None
        return "uncertain" if state == "launching" else "busy"

    def _recover(self) -> str | None:
        pending = self.bus.state("spoke-pending").get(self._pending_key)
        if pending is None or not (record := json.loads(pending.value)):
            return None
        event = Envelope.from_dict(record["event"])
        entry = self.bus.state("spoke-work").get(claim_key(event))
        state = json.loads(entry.value)
        status = state["state"]
        if status == "started":
            self._publish(Envelope.from_dict(state["event"]))
        elif status == "dead_pending" or (status == "ready" and record["attempt"] >= self.config.policy.max_deliver):
            self._finish_dead(event, record["attempt"])
            status = "dead"
        elif status == "launching":
            return "uncertain"
        elif status == "claimed":
            return "busy"
        self._clear_pending(pending.revision)
        return None if status == "ready" else status

    def _apply(self, delivery: Delivery, heartbeat: presence.Heartbeat) -> str:
        event = delivery.event
        claims = self.bus.state("spoke-work")
        key = claim_key(event)
        entry = claims.get(key)
        prior = json.loads(entry.value) if entry else {}
        if prior.get("state") == "started":
            self._publish(Envelope.from_dict(prior["event"]))
            self._clear_current(event)
            delivery.ack()
            return "started"
        if waiting := self._waiting(prior):
            delivery.nak(self.config.policy.ack_wait_ms)
            return waiting
        if prior.get("state") in ("dead", "withdrawn"):
            delivery.term()
            return prior["state"]
        if prior.get("state") == "dead_pending":
            return self._exhaust(delivery)
        if entry and prior["state"] not in ("ready", "claimed", "launching"):
            delivery.nak(self.config.policy.ack_wait_ms)
            return "busy"
        value = json.dumps({"owner": self.config.hive, "state": "claimed"}).encode()
        try:
            revision = claims.update(key, value, entry.revision) if entry else claims.create(key, value)
        except StateConflict:
            delivery.nak(self.config.policy.ack_wait_ms)
            return "busy"
        try:
            pending_revision = self._reserve(delivery)
        except Exception:
            claims.update(key, json.dumps({"state": "ready"}).encode(), revision)
            raise
        try:
            started = self._spawn(event, key, revision, heartbeat)
        except Exception:
            current = json.loads(claims.get(key).value)
            if current["state"] == "ready" and delivery.attempt < self.config.policy.max_deliver:
                self._clear_pending(pending_revision)
            raise
        if started is None:
            self._clear_pending(pending_revision)
            delivery.term()
            return "withdrawn"
        self._publish(started)
        self._clear_pending(pending_revision)
        delivery.ack()
        return "started"

    def _spawn(self, event: Envelope, key: str, revision: int, heartbeat: presence.Heartbeat) -> Envelope | None:
        claims = self.bus.state("spoke-work")
        try:
            request = self.tasks.request(event)
            if request is not None:
                execution_id = request.task["execution_id"]
                generation = request.task["execution_generation"]
                task_generation = request.task["claim_generation"]
            heartbeat.check()
        except Exception:
            claims.update(key, json.dumps({"state": "ready"}).encode(), revision)
            raise
        if request is None:
            claims.update(key, json.dumps({"state": "withdrawn"}).encode(), revision)
            return None
        intent = json.dumps({"state": "launching", "owner": self.config.hive, "name": request.name}).encode()
        revision = claims.update(key, intent, revision)
        request = replace(request, task={**request.task, "claim_fence": {"key": key, "revision": revision}})
        execution_events.prepare(self.bus, event.swarm, request.name, generation)
        outcome = self.runtime.spawn(request)
        if not outcome.ok:
            claims.update(key, json.dumps({"state": "ready"}).encode(), revision)
            raise RuntimeError
        placed = outcome.value
        started = Envelope(
            id=f"started:{key}",
            type="execution.started",
            swarm=event.swarm,
            entity="execution",
            key=execution_id,
            version=generation,
            time=time.time_ns() // 1_000_000,
            author=self.config.hive,
            data={
                "task": event.key,
                "task_generation": task_generation,
                "claim_key": key,
                "claim_revision": revision,
                "generation": generation,
                "lane": self.config.lane,
                "hive": self.config.hive,
                "name": request.name,
                "pane_id": placed.pane_id,
                "harness": placed.harness,
                "account": placed.account,
            },
            cause=event.id,
        )
        claims.update(
            key,
            json.dumps({"state": "started", "claim_revision": revision, "event": started.to_dict()}).encode(),
            revision,
        )
        return started

    def _publish(self, event: Envelope) -> None:
        execution_events.started(self.bus, event)

    def _clear_current(self, event: Envelope) -> None:
        pending = self.bus.state("spoke-pending").get(self._pending_key)
        if pending and json.loads(pending.value).get("event") == event.to_dict():
            self._clear_pending(pending.revision)

    def _finish_dead(self, event: Envelope, attempt: int) -> None:
        claims = self.bus.state("spoke-work")
        key = claim_key(event)
        entry = claims.get(key)
        prior = json.loads(entry.value)
        if prior["state"] == "dead_pending":
            letter = Envelope.from_dict(prior["event"])
            revision = entry.revision
        elif prior["state"] == "ready":
            letter = dead_letter(event, readiness.queue_name(event.swarm, self.config.lane), attempt)
            value = json.dumps({"state": "dead_pending", "event": letter.to_dict()}).encode()
            revision = claims.update(key, value, entry.revision)
        else:
            raise StateConflict
        self.bus.dead.publish(f"dead.{event.swarm}", letter)
        self.tasks.reopen(event)
        claims.update(key, json.dumps({"state": "dead"}).encode(), revision)

    def _exhaust(self, delivery: Delivery) -> str:
        self._finish_dead(delivery.event, delivery.attempt)
        self._clear_current(delivery.event)
        delivery.term()
        return "dead"
