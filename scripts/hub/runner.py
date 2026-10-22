import argparse
import hashlib
import os
import signal
import socket
import sys
import traceback

from scripts.events.bus import Bus, Delivery, StateConflict, StateStore
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.factory import open_bus
from scripts.hub import lease, registry
from scripts.hub.registry import Reactor, Registry
from scripts.scheduler.process_timer import wait as scheduled_wait

VERSIONS = "hub-versions"
IDLE_S = 5.0
BATCH = 32
FETCH_TIMEOUT_MS = 500
LEASE_TTL_MS = 15_000


def handled_key(reactor: Reactor, event: Envelope) -> str:
    digest = hashlib.sha256(f"{event.swarm}\0{event.key}".encode()).hexdigest()
    return f"{reactor.name}.{event.entity}.{digest}"


def record(store: StateStore, key: str, version: int) -> None:
    entry = store.get(key)
    if entry is not None and int(entry.value) >= version:
        return
    try:
        if entry is None:
            store.create(key, str(version).encode())
        else:
            store.update(key, str(version).encode(), entry.revision)
    except StateConflict:
        record(store, key, version)


# A singleton lease renews only between deliveries, so one singleton handler must finish within lease_ttl_ms.
class HubRunner:
    def __init__(
        self,
        bus: Bus,
        reactors: Registry,
        holder: str,
        clock: lease.Clock = lease.wall_ms,
        lease_ttl_ms: int = LEASE_TTL_MS,
    ):
        self._reactors = list(reactors)
        self._versions = bus.state(VERSIONS)
        self._consumers = {r.name: bus.events.consumer(registry.durable(r), r.subject) for r in self._reactors}
        leases = bus.state(lease.BUCKET)
        self._leases = {
            r.name: lease.Lease(leases, f"reactor.{r.name}", holder, lease_ttl_ms, clock)
            for r in self._reactors
            if r.singleton
        }

    def run_once(self, batch: int = BATCH, timeout_ms: int = FETCH_TIMEOUT_MS) -> dict[str, list[str]]:
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
        if reactor.poll is not None:
            try:
                reactor.poll(held.acquire if held is not None else lambda: True)
            except Exception:
                print(f"hub: reactor {reactor.name} timer callback failed", file=sys.stderr)
                traceback.print_exc()
                outcomes.append("failed")
        return outcomes

    def _deliver(self, reactor: Reactor, delivery: Delivery) -> str:
        event = delivery.event
        key = handled_key(reactor, event)
        entry = self._versions.get(key)
        if entry is not None and int(entry.value) >= event.version:
            delivery.ack()
            return "skipped"
        try:
            reactor.handler(event)
        except Exception:
            print(f"hub: reactor {reactor.name} failed on event {event.id}", file=sys.stderr)
            traceback.print_exc()
            delivery.nak()
            return "failed"
        record(self._versions, key, event.version)
        delivery.ack()
        return "handled"


def _run(hub: HubRunner, once: bool) -> int:
    while True:
        passes = hub.run_once()
        for name, outcomes in passes.items():
            for outcome in outcomes:
                print(f"{name}: {outcome}")
        if once:
            return 0
        if all(outcomes == ["standby"] for outcomes in passes.values()):
            scheduled_wait(IDLE_S)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agentihooks hub")
    parser.add_argument("action", choices=("run",))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    bus = open_bus(BusConfig.from_environ(os.environ))
    hub = HubRunner(bus, registry.configured(bus), os.environ.get("HOSTNAME") or socket.gethostname())
    previous = signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        return {"run": _run}[args.action](hub, args.once)
    finally:
        hub.stop()
        bus.close()
        signal.signal(signal.SIGTERM, previous)
