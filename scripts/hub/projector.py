import os
import re
import socket
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from scripts.events import catalogue, factory
from scripts.events.bus import Consumer
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.hub import registry
from scripts.hub.registry import Reactor
from scripts.hub.runner import retry_delay
from scripts.scheduler.process_interval import Interval
from scripts.swarm_ledger import ledger_link

SUBJECT = "swarm.>"
BATCH = 32
FETCH_TIMEOUT_MS = 500
REOPEN_S = 5.0
PULL_S = 0.05
_SAFE = re.compile(r"[a-z0-9]+")


def identity(host: str, port: int) -> str:
    return "-".join(_SAFE.findall(f"{host}-{port}".lower()))


class Projector:
    def __init__(self, repository, hub, name: str, config: BusConfig, clock=time.monotonic):
        self.repository = repository
        self.hub = hub
        self.name = name
        self.config = config
        self.clock = clock
        self.reactor = Reactor(f"projector-{name}", SUBJECT, self.handle)
        self.bus = None
        self.consumer = None
        self.reopen_at = 0.0

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
                delivery.nak(retry_delay(retry_ms, delivery.attempt))
                outcomes.append("failed")
            else:
                delivery.ack()
                outcomes.append("handled")
        return outcomes

    def pull(self) -> None:
        if self.consumer is None and self.clock() < self.reopen_at:
            return
        try:
            if self.consumer is None:
                self.bus = factory.open_bus(self.config)
                reactor = self.reactor
                self.consumer = self.bus.events.consumer(registry.durable(reactor), reactor.subject, reactor.policy)
            self.pass_once(self.consumer, self.reactor.retry_ms)
        except Exception as exc:
            sys.stderr.write(f"ledger projector: {exc}\n")
            self.close()
            self.reopen_at = self.clock() + REOPEN_S

    def close(self) -> None:
        if self.bus is not None:
            self.bus.close()
        self.bus = None
        self.consumer = None


@contextmanager
def running(repository, hub, environ: Mapping[str, str] = os.environ) -> Iterator[Projector | None]:
    config = BusConfig.from_environ(environ)
    if config.backend == "memory":
        yield None
        return
    host = environ.get("HOSTNAME") or socket.gethostname()
    projector = Projector(repository, hub, identity(host, ledger_link.address(environ)[1]), config)
    scheduled = Interval(projector.pull, every=PULL_S)
    scheduled.start()
    try:
        yield projector
    finally:
        try:
            scheduled.close()
        finally:
            projector.close()
