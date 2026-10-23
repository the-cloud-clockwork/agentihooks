import hashlib
import json
import sys
import traceback
from collections.abc import Callable

from scripts.events.bus import Bus, StateConflict
from scripts.events.config import BusConfig, QueuePolicy
from scripts.events.envelope import Envelope
from scripts.hive.spoke import claim_key
from scripts.hub import lease
from scripts.hub.registry import Reactor
from scripts.swarm import readiness
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import connect

INTERVAL_MS = 60_000
BUCKET = "hub-readiness"
CLAIMS = "spoke-work"
READY = {"state": "ready"}
WITHDRAWN = {"state": "withdrawn", "by": readiness.AUTHOR}

Load = Callable[[str], readiness.Snapshot | None]


def offers_key(slug: str) -> str:
    return hashlib.sha256(slug.encode()).hexdigest()


def ledger_view(store, ledger) -> Load:
    from scripts.swarm import freeze, tick

    def load(slug: str) -> readiness.Snapshot | None:
        if store.config(slug).state != "running":
            return None
        doc = freeze.watched(slug, store, ledger, ledger.state(slug))
        return tick.snapshot(slug, store, {t["id"]: t for t in doc["tasks"]}, doc)

    return load


class Readiness:
    def __init__(
        self,
        bus: Bus,
        store=None,
        load: Load | None = None,
        clock: lease.Clock = lease.wall_ms,
        policy: QueuePolicy = QueuePolicy(),
    ):
        self._bus = bus
        self._store = store
        self._load = load
        self._clock = clock
        self._policy = policy
        self._offers = bus.state(BUCKET)
        self._claims = bus.state(CLAIMS)
        self._last: int | None = None

    def reactor(self) -> Reactor:
        return Reactor("readiness", "swarm.*.readiness.requested", lambda _: None, singleton=True, poll=self.poll)

    def poll(self, guard: Callable[[], bool] = lambda: True) -> None:
        now = self._clock()
        if self._last is not None and now < self._last + INTERVAL_MS:
            return
        if not guard():
            return
        self._last = now
        if self._store is None:
            if BusConfig.from_environ().backend == "memory":
                return
            self._store = connect()
        self._load = self._load or ledger_view(self._store, LedgerClient())
        for slug in self._store.slugs():
            if not guard():
                return
            try:
                view = self._load(slug)
                if view is not None:
                    self.offer(view, now)
            except Exception:
                print(f"hub: readiness pass for swarm {slug} failed", file=sys.stderr)
                traceback.print_exc()

    def offer(self, view: readiness.Snapshot, now: int) -> list[str]:
        live = {event.id: event for event in self.outstanding(view.slug) if not self._settled(event, view)}
        placed = []
        for event in readiness.offers(view, now):
            if event.id in live or not self._lift(event):
                continue
            if self._bus.queue(readiness.queue_name(view.slug, event.data["lane"]), self._policy).offer(event):
                placed.append(event.id)
                live[event.id] = event
        self._save(view.slug, list(live.values()))
        return placed

    def outstanding(self, slug: str) -> list[Envelope]:
        entry = self._offers.get(offers_key(slug))
        return [Envelope.from_dict(raw) for raw in json.loads(entry.value)] if entry else []

    def _settled(self, event: Envelope, view: readiness.Snapshot) -> bool:
        key = claim_key(event)
        entry = self._claims.get(key)
        if entry is not None and json.loads(entry.value) != READY:
            return True
        if not readiness.withdrawal(event, view):
            return False
        value = json.dumps(WITHDRAWN).encode()
        try:
            if entry is None:
                self._claims.create(key, value)
            else:
                self._claims.update(key, value, entry.revision)
        except StateConflict:
            return False
        return True

    def _lift(self, event: Envelope) -> bool:
        entry = self._claims.get(claim_key(event))
        if entry is None or json.loads(entry.value) != WITHDRAWN:
            return True
        try:
            self._claims.update(entry.key, json.dumps(READY).encode(), entry.revision)
        except StateConflict:
            return False
        return True

    def _save(self, slug: str, events: list[Envelope]) -> None:
        key = offers_key(slug)
        value = json.dumps([event.to_dict() for event in events]).encode()
        entry = self._offers.get(key)
        try:
            if entry is None:
                self._offers.create(key, value)
            else:
                self._offers.update(key, value, entry.revision)
        except StateConflict:
            print(f"hub: readiness offers for swarm {slug} changed under the pass", file=sys.stderr)
