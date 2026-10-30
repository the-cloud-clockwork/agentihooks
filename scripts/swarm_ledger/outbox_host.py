from __future__ import annotations

import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from scripts.events import factory
from scripts.events.config import BusConfig
from scripts.scheduler.process_interval import Interval
from scripts.swarm_ledger.repository import outbox

INTERVAL = 1.0
CLOSE_S = 5.0


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
    if config.backend == "memory" or getattr(repository, "remote", False):
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


class Effects:
    """Runs the side effects ledger writes left held, on its own thread, each time a write or the watch wakes it."""

    def __init__(self, drain: Callable[[], object]) -> None:
        self.drain = drain
        self.due = threading.Event()
        self.stopping = False
        self.thread = threading.Thread(target=self.run, name="ledger-side-effects", daemon=True)

    def wake(self) -> None:
        self.due.set()

    def run(self) -> None:
        while True:
            self.due.wait()
            self.due.clear()
            if self.stopping:
                return
            try:
                self.drain()
            except Exception as exc:
                sys.stderr.write(f"ledger side effects: {exc}\n")

    def close(self) -> None:
        self.stopping = True
        self.due.set()
        self.thread.join(CLOSE_S)


@contextmanager
def effects(drain: Callable[[], object]) -> Iterator[Effects]:
    worker = Effects(drain)
    worker.thread.start()
    try:
        yield worker
    finally:
        worker.close()
