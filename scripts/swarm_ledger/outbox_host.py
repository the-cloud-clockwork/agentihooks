from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import partial
from typing import Any

from scripts.events import factory
from scripts.events.config import BusConfig
from scripts.scheduler.process_interval import Interval
from scripts.swarm_ledger.repository import outbox

INTERVAL = 1.0
PRUNE_EVERY = 30.0


class Relay:
    def __init__(self, repository, config: BusConfig):
        self.repository, self.config = repository, config
        self.bus = None

    def publish(self) -> None:
        try:
            if self.bus is None:
                self.bus = factory.open_bus(self.config)
            outbox.relay(self.repository, self.bus.events)
        except Exception as exc:
            sys.stderr.write(f"ledger bus relay: {exc}\n")
            self.close()

    def close(self) -> None:
        if self.bus is not None:
            self.bus.close()
            self.bus = None


def prune(repository: Any, config: BusConfig) -> None:
    """Drop sent rows past the dedupe window and unsent rows past the cap, whether or not a relay ran."""
    try:
        outbox.prune(repository, config.dedupe_window_ms)
        dropped = outbox.cap(repository, outbox.MAX_UNSENT)
    except Exception as exc:
        sys.stderr.write(f"ledger outbox prune: {exc}\n")
        return
    if dropped:
        sys.stderr.write(f"ledger outbox prune: dropped {dropped} unsent rows past {outbox.MAX_UNSENT}\n")


@contextmanager
def scheduled(callback: Callable[[], None], every: float) -> Iterator[None]:
    """Run the callback on its own interval thread until the block ends."""
    task = Interval(callback, every=every)
    task.start()
    try:
        yield
    finally:
        task.close()


@contextmanager
def running(repository: Any, interval: float = INTERVAL, prune_every: float = PRUNE_EVERY) -> Iterator[None]:
    """Prune the outbox on every backend and relay it to the bus unless the bus is in memory."""
    config = BusConfig.from_environ()
    with scheduled(partial(prune, repository, config), prune_every):
        if config.backend == "memory":
            yield
            return
        relay = Relay(repository, config)
        try:
            with scheduled(relay.publish, interval):
                yield
        finally:
            relay.close()
