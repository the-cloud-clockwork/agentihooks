import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from scripts.events.bus import Bus, Delivery, StateConflict
from scripts.events.config import QueuePolicy
from scripts.events.dead import dead_letter
from scripts.events.envelope import Envelope
from scripts.events.subjects import subject
from scripts.hive import admission, headroom
from scripts.swarm import readiness
from scripts.swarm_v2.runtime.base import Runtime, SpawnRequest


class Tasks(Protocol):
    def request(self, event: Envelope) -> SpawnRequest | None: ...

    def reopen(self, event: Envelope) -> None: ...


@dataclass(frozen=True)
class SpokeConfig:
    swarm: Any
    lane: str
    hive: str
    policy: QueuePolicy = QueuePolicy()


def claim_key(event: Envelope) -> str:
    return hashlib.sha256(json.dumps([event.swarm, event.key, event.version]).encode()).hexdigest()


def measured() -> dict | None:
    return headroom.measure(0, time.time_ns() // 1_000_000)


class Spoke:
    def __init__(
        self,
        config: SpokeConfig,
        bus: Bus,
        runtime: Runtime,
        tasks: Tasks,
        measure: Callable[[], dict | None] = measured,
    ):
        self.config, self.bus, self.runtime, self.tasks, self.measure = config, bus, runtime, tasks, measure

    def poll(self, timeout_ms: int = 0) -> str:
        if admission.room(self.measure()).room < 1:
            return "held"
        queue = self.bus.queue(readiness.queue_name(self.config.swarm.slug, self.config.lane), self.config.policy)
        delivery = queue.take(timeout_ms)
        if delivery is None:
            return "empty"
        try:
            return self._apply(delivery)
        except Exception:
            try:
                if delivery.attempt >= self.config.policy.max_deliver:
                    return self._exhaust(delivery)
            except Exception:
                pass
            steps = self.config.policy.backoff_ms or (self.config.policy.ack_wait_ms,)
            delivery.nak(steps[min(delivery.attempt - 1, len(steps) - 1)])
            return "retry"

    def _apply(self, delivery: Delivery) -> str:
        event = delivery.event
        claims = self.bus.state("spoke-work")
        key = claim_key(event)
        entry = claims.get(key)
        prior = json.loads(entry.value) if entry else {}
        if prior.get("state") == "started":
            return self._announce(delivery, Envelope.from_dict(prior["event"]))
        if prior.get("state") == "dead":
            delivery.term()
            return "dead"
        if prior.get("state") == "dead_pending":
            return self._exhaust(delivery)
        if prior.get("state") == "withdrawn":
            delivery.term()
            return "withdrawn"
        if entry and prior["state"] != "ready":
            delivery.nak(self.config.policy.ack_wait_ms)
            return "busy"
        value = json.dumps({"owner": self.config.hive, "state": "claimed"}).encode()
        try:
            revision = claims.update(key, value, entry.revision) if entry else claims.create(key, value)
        except StateConflict:
            delivery.nak(self.config.policy.ack_wait_ms)
            return "busy"
        try:
            started = self._spawn(event, key)
        except Exception:
            claims.update(key, json.dumps({"state": "ready"}).encode(), revision)
            raise
        if started is None:
            claims.update(key, json.dumps({"state": "withdrawn"}).encode(), revision)
            delivery.term()
            return "withdrawn"
        claims.update(key, json.dumps({"state": "started", "event": started.to_dict()}).encode(), revision)
        return self._announce(delivery, started)

    def _spawn(self, event: Envelope, key: str) -> Envelope | None:
        request = self.tasks.request(event)
        if request is None:
            return None
        outcome = self.runtime.spawn(request)
        if not outcome.ok:
            raise RuntimeError(outcome.detail)
        placed = outcome.value
        return Envelope(
            id=f"started:{key}",
            type="execution.started",
            swarm=event.swarm,
            entity="execution",
            key=request.name,
            version=event.version,
            time=time.time_ns() // 1_000_000,
            author=self.config.hive,
            data={
                "task": event.key,
                "lane": self.config.lane,
                "hive": self.config.hive,
                "name": request.name,
                "pane_id": placed.pane_id,
                "harness": placed.harness,
                "account": placed.account,
            },
            cause=event.id,
        )

    def _announce(self, delivery: Delivery, event: Envelope) -> str:
        self.bus.events.publish(subject(event.swarm, "execution", "started"), event)
        delivery.ack()
        return "started"

    def _exhaust(self, delivery: Delivery) -> str:
        event = delivery.event
        claims = self.bus.state("spoke-work")
        key = claim_key(event)
        entry = claims.get(key)
        prior = json.loads(entry.value) if entry else {}
        if prior.get("state") == "dead_pending":
            letter = Envelope.from_dict(prior["event"])
            revision = entry.revision
        else:
            letter = dead_letter(event, readiness.queue_name(event.swarm, self.config.lane), delivery.attempt)
            value = json.dumps({"state": "dead_pending", "event": letter.to_dict()}).encode()
            revision = claims.update(key, value, entry.revision) if entry else claims.create(key, value)
        self.bus.dead.publish(f"dead.{event.swarm}", letter)
        self.tasks.reopen(event)
        claims.update(key, json.dumps({"state": "dead"}).encode(), revision)
        delivery.term()
        return "dead"
