import os
import re
import socket
import sys
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from scripts.events import catalogue, factory
from scripts.events.bus import Consumer
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.hub import registry
from scripts.hub.registry import Reactor

SUBJECT = "swarm.>"
BATCH = 32
FETCH_TIMEOUT_MS = 500
REOPEN_S = 5.0
STOP_S = 5.0
_UNSAFE = re.compile(r"[^a-z0-9-]+")


def identity(host: str, port: int) -> str:
    return _UNSAFE.sub("-", f"{host}-{port}".lower())


# Each ledger server follows under its own durable, so every serving process sees every event for its pages.
class Projector:
    def __init__(self, repository, hub, name: str):
        self.repository = repository
        self.hub = hub
        self.name = name
        self.worker = None

    def reactor(self) -> Reactor:
        return Reactor(f"projector-{self.name}", SUBJECT, self.handle)

    def handle(self, event: Envelope) -> None:
        if event.type in catalogue.LEDGER and event.swarm in self.hub.watched():
            self.repository.current(event.swarm)

    def pass_once(self, consumer: Consumer, retry_ms: tuple[int, ...]) -> list[str]:
        outcomes = []
        for delivery in consumer.fetch(BATCH, FETCH_TIMEOUT_MS):
            try:
                self.handle(delivery.event)
            except Exception as exc:
                sys.stderr.write(f"ledger projector: event {delivery.event.id}: {exc}\n")
                delivery.nak(registry.retry_delay(retry_ms, delivery.attempt))
                outcomes.append("failed")
            else:
                delivery.ack()
                outcomes.append("handled")
        return outcomes

    def follow(self, config: BusConfig, stopped: threading.Event) -> None:
        reactor = self.reactor()
        while not stopped.is_set():
            bus = None
            try:
                bus = factory.open_bus(config)
                consumer = bus.events.consumer(registry.durable(reactor), reactor.subject, reactor.policy)
                while not stopped.is_set():
                    self.pass_once(consumer, reactor.retry_ms)
            except Exception as exc:
                sys.stderr.write(f"ledger projector: {exc}\n")
            finally:
                if bus is not None:
                    bus.close()
            stopped.wait(REOPEN_S)


@contextmanager
def running(repository, hub, port: int, environ: Mapping[str, str] = os.environ) -> Iterator[Projector | None]:
    config = BusConfig.from_environ(environ)
    if config.backend == "memory":
        yield None
        return
    projector = Projector(repository, hub, identity(environ.get("HOSTNAME") or socket.gethostname(), port))
    stopped = threading.Event()
    projector.worker = threading.Thread(
        target=projector.follow, args=(config, stopped), name="ledger-projector", daemon=True
    )
    projector.worker.start()
    try:
        yield projector
    finally:
        stopped.set()
        projector.worker.join(STOP_S)
