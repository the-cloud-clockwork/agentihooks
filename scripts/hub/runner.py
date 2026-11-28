import argparse
import functools
import hashlib
import os
import signal
import socket
import sys
import threading
import traceback
from dataclasses import replace
from pathlib import Path
from time import sleep
from typing import TYPE_CHECKING, Any, NoReturn, cast

from scripts.events.bus import Bus, Delivery, StateConflict, StateStore
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.factory import open_bus
from scripts.hive import auth, buckets
from scripts.hub import exits, lease, registry, slots
from scripts.hub.admission import Admission
from scripts.hub.registry import Reactor, Registry
from scripts.scheduler.process_interval import Interval
from scripts.scheduler.process_timer import wait as scheduled_wait
from scripts.swarm import readiness, redis_grace, store
from scripts.swarm_ledger import ledger_link, storage
from scripts.swarm_ledger.repository import postgres

if TYPE_CHECKING:
    from redis import Redis

VERSIONS = "hub-versions"
IDLE_S = 5.0
BATCH = 32
FETCH_TIMEOUT_MS = 500
LEASE_TTL_MS = 15_000
LEASE_RENEW_S = LEASE_TTL_MS / 1000 / 3
CREDENTIAL_ENV = "AGENTIHOOKS_CONTROLLER_CREDENTIAL"
SERVING_ENV = "AGENTIHOOKS_HUB_SERVING_FILE"
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


# A hung handler keeps its singleton lease until the process stops, because a thread renews held leases.
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
        self._holding: set[str] = set()
        self._lock = threading.Lock()
        self.stopped = threading.Event()

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

    def renew(self) -> None:
        with self._lock:
            held = sorted(self._holding)
        for name in held:
            self._hold(name)

    def _hold(self, name: str) -> bool:
        with self._lock:
            if self.stopped.is_set():
                return False
            held = self._leases[name].acquire()
            if held != (name in self._holding):
                print(f"{name}: lease {'taken' if held else 'lost'} by {self._leases[name].holder}")
                if held:
                    self._holding.add(name)
                else:
                    self._holding.discard(name)
            return held

    def stop(self) -> None:
        with self._lock:
            self.stopped.set()
            self._holding.clear()
        for name, held in self._leases.items():
            try:
                held.release()
            except Exception:
                print(f"hub: reactor {name} lease release failed", file=sys.stderr)
                traceback.print_exc()

    def _pass(self, reactor: Reactor, batch: int, timeout_ms: int) -> list[str]:
        singleton = reactor.name in self._leases
        guard = functools.partial(self._hold, reactor.name) if singleton else lambda: True
        if not guard():
            return ["standby"]
        outcomes = []
        for delivery in self._consumers[reactor.name].fetch(batch, timeout_ms):
            if singleton and outcomes and not guard():
                delivery.nak()
                outcomes.append("standby")
                continue
            outcomes.append(self._deliver(reactor, delivery))
        if reactor.poll is not None:
            try:
                reactor.poll(guard)
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


def _idle(outage: redis_grace.Outage) -> None:
    try:
        scheduled_wait(IDLE_S)
    except store.store_errors() as exc:
        if not outage.tolerated():
            raise
        print(
            f"hub: Redis refused the idle wait inside the Redis grace window, waiting on the local clock: {exc}",
            file=sys.stderr,
        )
        sleep(IDLE_S)
    else:
        outage.ended()


def _run(hub: HubRunner, once: bool) -> int:
    outage = redis_grace.Outage.from_environ()
    while True:
        passes = hub.run_once()
        for name, outcomes in passes.items():
            for outcome in outcomes:
                print(f"{name}: {outcome}")
        if once:
            return 0
        if all(outcomes == ["standby"] for outcomes in passes.values()) or any(
            "failed" in outcomes for outcomes in passes.values()
        ):
            _idle(outage)
        else:
            outage.ended()


def _issued_credential(holder: str) -> tuple["Redis", str] | None:
    if not ledger_link.remote():
        return None
    redis = store.redis_client()
    return redis, auth.issue_service(redis, f"hub-{holder}")


def _renew_forever(redis: "Redis", credential: str) -> None:
    while True:
        try:
            scheduled_wait(KEEP_S)
        except Exception:
            print("hub: service credential renewal wait failed", file=sys.stderr)
            traceback.print_exc()
            sleep(KEEP_S)
        try:
            auth.keep_service(redis, credential)
        except Exception:
            print("hub: service credential renewal failed", file=sys.stderr)
            traceback.print_exc()


def _renewal(redis: "Redis", credential: str) -> threading.Thread:
    thread = threading.Thread(target=_renew_forever, args=(redis, credential), daemon=True)
    thread.start()
    return thread


def _renew_leases(hub: HubRunner) -> None:
    try:
        hub.renew()
    except Exception:
        print("hub: reactor lease renewal failed", file=sys.stderr)
        traceback.print_exc()


def _interrupted(*_: object) -> NoReturn:
    sys.exit(0)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agentihooks hub")
    parser.add_argument("action", choices=("run",))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    serving = os.environ.get(SERVING_ENV)
    if serving:
        Path(serving).unlink(missing_ok=True)
    if cast(Any, ledger_link).remote() and not storage.shared():
        print(
            f"hub: a hosted hub writes task claims in process over the shared database and needs {postgres.URL}",
            file=sys.stderr,
        )
        return 2
    holder = os.environ.get("HOSTNAME") or socket.gethostname()
    issued = _issued_credential(holder)
    if issued:
        os.environ[CREDENTIAL_ENV] = issued[1]
        _renewal(*issued)
    config = BusConfig.from_environ(os.environ)
    bus = open_bus(replace(config, reconnect=True) if config.backend == "nats" else config)
    buckets.provision(bus)
    source = None if config.backend == "memory" else readiness.Source()
    slots.serve(bus, admitter=Admission(source=source))
    exits.serve(bus)
    if serving:
        Path(serving).touch()
    hub = HubRunner(bus, registry.configured(bus, source=source), holder)
    keeper = None if args.once else Interval(functools.partial(_renew_leases, hub), every=LEASE_RENEW_S)
    if keeper is not None:
        keeper.start()
    previous = signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    interrupted = signal.signal(signal.SIGINT, _interrupted)
    try:
        return {"run": _run}[args.action](hub, args.once)
    finally:
        hub.stop()
        if keeper is not None:
            keeper.close()
        bus.close()
        signal.signal(signal.SIGTERM, previous)
        signal.signal(signal.SIGINT, interrupted)
