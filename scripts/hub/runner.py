import argparse
import hashlib
import os
import signal
import socket
import sys
import time
import traceback

from scripts.events.bus import Bus, Delivery, StateConflict, StateStore
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.factory import open_bus
from scripts.hub import lease, registry
from scripts.hub.registry import Reactor, Registry

VERSIONS = "hub-versions"
IDLE_S = 0.5


def handled_key(reactor: Reactor, event: Envelope) -> str:
    digest = hashlib.sha256(f"{event.swarm}\0{event.key}".encode()).hexdigest()[:32]
    return f"{reactor.name}.{event.entity}.{digest}"


def _handled(store: StateStore, key: str) -> int:
    entry = store.get(key)
    return -1 if entry is None else int(entry.value)


def _record(store: StateStore, key: str, version: int) -> None:
    while True:
        entry = store.get(key)
        if entry is not None and int(entry.value) >= version:
            return
        try:
            if entry is None:
                store.create(key, str(version).encode())
            else:
                store.update(key, str(version).encode(), entry.revision)
            return
        except StateConflict:
            continue


class HubRunner:
    def __init__(
        self, bus: Bus, reactors: Registry, holder: str, clock: lease.Clock = lease.wall_ms, lease_ttl_ms: int = 15_000
    ):
        self._bus = bus
        self._reactors = list(reactors)
        self._versions = bus.state(VERSIONS)
        self._consumers = {r.name: bus.events.consumer(registry.durable(r), r.subject) for r in self._reactors}
        leases = bus.state(lease.BUCKET)
        self._leases = {
            r.name: lease.Lease(leases, f"reactor.{r.name}", holder, lease_ttl_ms, clock)
            for r in self._reactors
            if r.singleton
        }

    def run_once(self, batch: int = 32, timeout_ms: int = 500) -> dict[str, list[str]]:
        return {reactor.name: self._pass(reactor, batch, timeout_ms) for reactor in self._reactors}

    def stop(self) -> None:
        for held in self._leases.values():
            held.release()

    def _pass(self, reactor: Reactor, batch: int, timeout_ms: int) -> list[str]:
        held = self._leases.get(reactor.name)
        if held is not None and not held.acquire():
            return ["standby"]
        outcomes = []
        for delivery in self._consumers[reactor.name].fetch(batch, timeout_ms):
            if held is not None and outcomes and not held.acquire():
                delivery.nak()
                outcomes.append("standby")
                continue
            outcomes.append(self._deliver(reactor, delivery))
        return outcomes

    def _deliver(self, reactor: Reactor, delivery: Delivery) -> str:
        event = delivery.event
        key = handled_key(reactor, event)
        if _handled(self._versions, key) >= event.version:
            delivery.ack()
            return "skipped"
        try:
            reactor.handler(event)
        except Exception:
            print(f"hub: reactor {reactor.name} failed on event {event.id}", file=sys.stderr)
            traceback.print_exc()
            delivery.nak()
            return "failed"
        _record(self._versions, key, event.version)
        delivery.ack()
        return "handled"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agentihooks hub")
    parser.add_argument("action", choices=("run",))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    bus = open_bus(BusConfig.from_environ(os.environ))
    hub = HubRunner(bus, registry.REACTORS, os.environ.get("HOSTNAME") or socket.gethostname())
    previous = signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        while True:
            passes = hub.run_once()
            for name, outcomes in passes.items():
                for outcome in outcomes:
                    print(f"{name}: {outcome}", flush=True)
            if args.once:
                return 0
            if all(outcomes == ["standby"] for outcomes in passes.values()):
                time.sleep(IDLE_S)
    finally:
        hub.stop()
        bus.close()
        signal.signal(signal.SIGTERM, previous)
