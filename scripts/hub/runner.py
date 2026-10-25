import argparse
import hashlib
import os
import signal
import socket
import sys
import threading
import traceback
from typing import TYPE_CHECKING

from scripts.events.bus import Bus, Delivery, StateConflict, StateStore
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.factory import open_bus
from scripts.hive import auth, buckets
from scripts.hub import lease, registry, slots
from scripts.hub.registry import Reactor, Registry
from scripts.scheduler.process_timer import wait as scheduled_wait
from scripts.swarm import store
from scripts.swarm_ledger import ledger_link

if TYPE_CHECKING:
    from redis import Redis

VERSIONS = "hub-versions"
IDLE_S = 5.0
BATCH = 32
FETCH_TIMEOUT_MS = 500
LEASE_TTL_MS = 15_000
CREDENTIAL_ENV = "AGENTIHOOKS_CONTROLLER_CREDENTIAL"
KEEP_S = auth.SERVICE_TTL_S / 3


def handled_key(reactor: Reactor, event: Envelope) -> str:
    digest = hashlib.sha256(f"{event.swarm}\0{event.key}".encode()).hexdigest()
    return f"{reactor.name}.{event.entity}.{digest}"


def retry_delay(retry_ms: tuple[int, ...], attempt: int) -> int:
    return retry_ms[min(attempt, len(retry_ms)) - 1] if retry_ms else 0


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
        self._bus = bus
        self._reactors = list(reactors)
        self._versions = bus.state(VERSIONS)
        self._consumers = {
            r.name: bus.events.consumer(registry.durable(r), r.subject, r.policy) for r in self._reactors
        }
        leases = bus.state(lease.BUCKET)
        self._leases = {
            r.name: lease.Lease(leases, f"reactor.{r.name}", holder, lease_ttl_ms, clock)
            for r in self._reactors
            if r.singleton
        }

    def run_once(self, batch: int = BATCH, timeout_ms: int = FETCH_TIMEOUT_MS) -> dict[str, list[str]]:
        passes = {}
        for reactor in self._reactors:
            try:
                passes[reactor.name] = self._pass(reactor, batch, timeout_ms)
            except Exception:
                print(f"hub: reactor {reactor.name} transport failed", file=sys.stderr)
                traceback.print_exc()
                passes[reactor.name] = ["failed"]
        return passes

    def stop(self) -> None:
        for name, held in self._leases.items():
            try:
                held.release()
            except Exception:
                print(f"hub: reactor {name} lease release failed", file=sys.stderr)
                traceback.print_exc()

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
            view = self._bus.for_swarm(event.swarm)
            if view is not self._bus:
                buckets.provision(view)
            reactor.handler(event)
        except Exception:
            print(f"hub: reactor {reactor.name} failed on event {event.id}", file=sys.stderr)
            traceback.print_exc()
            delivery.nak(retry_delay(reactor.retry_ms, delivery.attempt))
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


def _issued_credential(holder: str) -> tuple["Redis", str] | None:
    if not ledger_link.remote():
        return None
    redis = store.redis_client()
    return redis, auth.issue_service(redis, f"hub-{holder}")


def _renew_forever(redis: "Redis", credential: str) -> None:
    while True:
        scheduled_wait(KEEP_S)
        try:
            auth.keep_service(redis, credential)
        except Exception:
            print("hub: service credential renewal failed", file=sys.stderr)
            traceback.print_exc()


def _renewal(redis: "Redis", credential: str) -> threading.Thread:
    thread = threading.Thread(target=_renew_forever, args=(redis, credential), daemon=True)
    thread.start()
    return thread


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agentihooks hub")
    parser.add_argument("action", choices=("run",))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    holder = os.environ.get("HOSTNAME") or socket.gethostname()
    issued = _issued_credential(holder)
    if issued:
        os.environ[CREDENTIAL_ENV] = issued[1]
        _renewal(*issued)
    bus = open_bus(BusConfig.from_environ(os.environ))
    buckets.provision(bus)
    slots.serve(bus)
    hub = HubRunner(bus, registry.configured(bus), holder)
    previous = signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        return {"run": _run}[args.action](hub, args.once)
    finally:
        hub.stop()
        bus.close()
        signal.signal(signal.SIGTERM, previous)
