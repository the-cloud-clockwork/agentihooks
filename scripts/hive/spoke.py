import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from scripts.events.bus import Bus, StateConflict
from scripts.events.config import QueuePolicy
from scripts.events.envelope import Envelope
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
        event = delivery.event
        claims = self.bus.state("spoke-work")
        key = f"{event.swarm}:{event.key}:{event.version}"
        value = json.dumps({"owner": self.config.hive, "state": "claimed"}).encode()
        try:
            revision = claims.create(key, value)
        except StateConflict:
            delivery.nak(self.config.policy.ack_wait_ms)
            return "busy"
        request = self.tasks.request(event)
        if request is None:
            claims.update(key, json.dumps({"state": "withdrawn"}).encode(), revision)
            delivery.term()
            return "withdrawn"
        return "claimed"
