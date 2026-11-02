from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager

from scripts.events import factory
from scripts.events.config import BusConfig
from scripts.scheduler.process_interval import Interval
from scripts.swarm_ledger.repository import outbox

INTERVAL = 1.0


class Relay:
    def __init__(self, repository, config: BusConfig):
        self.repository, self.config = repository, config
        self.bus = None

    def publish(self) -> None:
        try:
            if self.bus is None:
                self.bus = factory.open_bus(self.config)
            outbox.relay(self.repository, self.bus.events)
            outbox.prune(self.repository, self.config.dedupe_window_ms)
        except Exception as exc:
            sys.stderr.write(f"ledger bus relay: {exc}\n")
            self.close()

    def close(self) -> None:
        if self.bus is not None:
            self.bus.close()
            self.bus = None


@contextmanager
def running(repository, interval: float = INTERVAL) -> Iterator[None]:
    config = BusConfig.from_environ()
    if config.backend == "memory":
        yield
        return
    relay = Relay(repository, config)
    scheduled = Interval(relay.publish, every=interval)
    scheduled.start()
    try:
        yield
    finally:
        try:
            scheduled.close()
        finally:
            relay.close()
