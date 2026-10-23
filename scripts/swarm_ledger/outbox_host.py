import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from scripts.events import factory
from scripts.events.config import BusConfig
from scripts.swarm_ledger.repository import outbox

INTERVAL = 1.0


def relay(repository, stopped: threading.Event, config: BusConfig, interval: float) -> None:
    bus = None
    try:
        while not stopped.is_set():
            try:
                if bus is None:
                    bus = factory.open_bus(config)
                outbox.relay(repository, bus.events)
            except Exception as exc:
                sys.stderr.write(f"ledger bus relay: {exc}\n")
            stopped.wait(interval)
    finally:
        if bus is not None:
            bus.close()


@contextmanager
def running(repository, interval: float = INTERVAL) -> Iterator[threading.Thread | None]:
    config = BusConfig.from_environ()
    if config.backend == "memory":
        yield None
        return
    stopped = threading.Event()
    worker = threading.Thread(target=relay, args=(repository, stopped, config, interval), daemon=True)
    worker.start()
    try:
        yield worker
    finally:
        stopped.set()
        worker.join()
