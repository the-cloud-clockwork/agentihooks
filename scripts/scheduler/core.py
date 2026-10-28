import json
import logging
import os
import socket
import time
import uuid
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Callable, TypeVar

from hooks._redis import redis_key
from scripts.events.bus import StateConflict
from scripts.scheduler.lease import Lease, check, dispatch_lock, verify_lease
from scripts.scheduler.model import Schedule, next_due
from scripts.scheduler.slots import CallableSlot, Slot, restore

if TYPE_CHECKING:
    from collections.abc import Mapping

    from scripts.events.bus import StateStore

_log = logging.getLogger(__name__)
BUCKET = "scheduler"
WRITE_TRIES = 64
T = TypeVar("T")


@dataclass(frozen=True)
class Signal:
    scheduler: "Scheduler"
    entry_id: str
    name: str

    def connect(self, slot: Slot | Callable) -> str:
        if callable(slot):
            slot = CallableSlot.from_callback(slot)
        connection_id = uuid.uuid4().hex
        self.scheduler.update(
            self.entry_id,
            lambda entry: entry["slots"].append({"id": connection_id, "signal": self.name, **slot.record()}),
        )
        return connection_id

    def disconnect(self, connection_id: str) -> None:
        def change(entry):
            entry["slots"] = [slot for slot in entry["slots"] if slot["id"] != connection_id]

        self.scheduler.update(self.entry_id, change)


def bucket(environ: "Mapping[str, str] | None" = None) -> str:
    from scripts.swarm_ledger import ledger_link

    env = os.environ if environ is None else environ
    member = "" if ledger_link.local_mode(env) else env.get("AGENTIHOOKS_HIVE_ID")
    return f"{BUCKET}-{member}" if member else BUCKET


@dataclass(frozen=True)
class Entry:
    scheduler: "Scheduler"
    id: str

    def record(self) -> dict:
        entry = self.scheduler.document()["entries"].get(self.id)
        if entry is None:
            raise KeyError(self.id)
        return entry

    record = property(record)

    @property
    def state(self) -> str:
        return self.record["state"]

    def start(self, *, now: float | None = None) -> None:
        now = time.time() if now is None else now

        def change(entry):
            if entry["state"] != "ready":
                raise ValueError("only a ready schedule can be started")
            entry["started_at"] = now
            if entry["kind"] == "cron":
                entry["due"] = next_due(entry["expression"], entry["timezone"], now)
            elif entry["delay"] is not None:
                entry["due"] = now + entry["delay"]
            entry["state"] = "active"

        self.scheduler.update(self.id, change)

    @property
    def timeout(self) -> Signal:
        return Signal(self.scheduler, self.id, "timeout")

    @property
    def limit_reached(self) -> Signal:
        return Signal(self.scheduler, self.id, "limit_reached")

    def elapsed(self, *, now: float | None = None) -> float:
        now = time.time() if now is None else now
        entry = self.record
        if entry["started_at"] is None:
            return 0
        end = entry.get("stopped_at", entry["due"] if entry["state"] == "fired" else now)
        return max(0, end - entry["started_at"])

    def stop(self, *, now: float | None = None) -> None:
        now = time.time() if now is None else now

        def change(entry):
            entry["state"] = "stopped"
            entry["stopped_at"] = now
            entry.pop("pending", None)

        self.scheduler.update(self.id, change)

    def cancel(self) -> None:
        def change(entry):
            entry["state"] = "cancelled"
            entry.pop("pending", None)

        self.scheduler.update(self.id, change)


class Scheduler:
    def __init__(self, state: "StateStore | None" = None, *, machine: str | None = None) -> None:
        self._state = state
        self.machine = machine or socket.gethostname()

    def state(self) -> "StateStore":
        if self._state is None:
            from scripts.events.scheduler_state import open_scheduler_state

            self._state = open_scheduler_state(bucket())
        return self._state

    state = property(state)

    def key(self, suffix: str) -> str:
        from scripts.swarm_ledger import ledger_link

        if not ledger_link.local_mode(os.environ):
            from scripts.swarm.keyspace import ROOT

            if member := os.environ.get("AGENTIHOOKS_HIVE_ID"):
                return f"{ROOT}:hive:{member}:scheduler:{self.machine}:{suffix}"
            return f"{ROOT}:scheduler:{self.machine}:{suffix}"
        return redis_key("scheduler", f"{self.machine}:{suffix}")

    def name(self, suffix: str) -> str:
        return self.key(suffix).replace(":", ".")

    def document(self) -> dict:
        return self._read()[0]

    def _read(self) -> tuple[dict, int | None]:
        from scripts.events.scheduler_state import read

        stored = read(self.state, self.name("entries"), self.key("entries"))
        document = json.loads(stored.value) if stored is not None and stored.value else {}
        document.setdefault("entries", {})
        document.setdefault("retired", {})
        return document, None if stored is None else stored.revision

    def change(self, change: Callable[[dict], T], *, lease: Lease | None = None) -> T:
        key = self.name("entries")
        for _ in range(WRITE_TRIES):
            document, revision = self._read()
            if lease is not None:
                check(document, lease)
            before = json.dumps(document, sort_keys=True)
            result = change(document)
            after = json.dumps(document, sort_keys=True)
            if after == before:
                return result
            try:
                if revision is None:
                    self.state.create(key, after.encode())
                else:
                    self.state.update(key, after.encode(), revision)
                return result
            except StateConflict:
                continue
        raise StateConflict(f"scheduler state {key!r} kept changing")

    def read(self, suffix: str) -> str:
        stored = self.state.get(self.name(suffix))
        return "" if stored is None else stored.value.decode()

    def put(self, suffix: str, value: str) -> None:
        key = self.name(suffix)
        for _ in range(WRITE_TRIES):
            stored = self.state.get(key)
            try:
                if stored is None:
                    self.state.create(key, value.encode())
                else:
                    self.state.update(key, value.encode(), stored.revision)
                return
            except StateConflict:
                continue
        raise StateConflict(f"scheduler state {key!r} kept changing")

    def get(self, entry_id: str) -> Entry:
        entry = Entry(self, entry_id)
        entry.record
        return entry

    def owned(self, owner: str) -> list[Entry]:
        entries = self.document()["entries"]
        return [Entry(self, entry_id) for entry_id, entry in entries.items() if entry["owner"] == owner]

    def records(self) -> list[dict]:
        return list(self.document()["entries"].values())

    def update(self, entry_id: str, change: Callable[[dict], None], *, lease: Lease | None = None) -> None:
        def apply(document):
            entry = document["entries"].get(entry_id)
            if entry is None:
                raise KeyError(entry_id)
            change(entry)

        self.change(apply, lease=lease)

    def timer(
        self, *, owner: str, payload: str, delay: float | None = None, at: float | None = None, now: float | None = None
    ) -> Entry:
        now = time.time() if now is None else now
        if (delay is None) == (at is None):
            raise ValueError("a timer needs either delay or at")
        if delay is not None and delay < 0:
            raise ValueError("delay must not be negative")
        due = at if at is not None else now + delay
        return self._new(Schedule("timer", owner, payload, due, delay))

    def stopwatch(self, *, owner: str, payload: str, limit: float, now: float | None = None) -> Entry:
        now = time.time() if now is None else now
        if limit < 0:
            raise ValueError("limit must not be negative")
        return self._new(Schedule("stopwatch", owner, payload, now + limit, limit))

    def interval(self, *, owner: str, payload: str, every: float, now: float | None = None) -> Entry:
        now = time.time() if now is None else now
        if every <= 0:
            raise ValueError("an interval must be positive")
        return self._new(Schedule("interval", owner, payload, now + every, every))

    def cron(
        self, *, owner: str, payload: str, expression: str, timezone: str = "UTC", now: float | None = None
    ) -> Entry:
        now = time.time() if now is None else now
        due = next_due(expression, timezone, now)
        return self._new(Schedule("cron", owner, payload, due, None, expression, timezone))

    def _new(self, schedule: Schedule) -> Entry:
        if not schedule.owner or not schedule.payload.strip():
            raise ValueError("a schedule needs an owner and payload")
        entry_id = uuid.uuid4().hex
        entry = {
            **asdict(schedule),
            "id": entry_id,
            "state": "ready",
            "started_at": None,
            "signal": "limit_reached" if schedule.kind == "stopwatch" else "timeout",
            "slots": [],
        }
        self.change(lambda document: document["entries"].__setitem__(entry_id, entry))
        return Entry(self, entry_id)

    @staticmethod
    def _claim(document: dict, now: float) -> int:
        fired = 0
        for entry in document["entries"].values():
            if entry["state"] != "active" or entry["due"] > now:
                continue
            entry["pending"] = {
                "id": uuid.uuid4().hex,
                "scheduled_at": entry["due"],
                "fired_at": now,
                "slots": [slot for slot in entry["slots"] if slot["signal"] == entry["signal"]],
                "delivered": [],
            }
            entry["state"] = "pending"
            fired += 1
        return fired

    def _deliver(self, entry_id: str, now: float, lease: Lease) -> None:
        entry = self.get(entry_id).record
        pending = entry.get("pending")
        if pending is None:
            _log.warning("scheduler skipped stale pending entry %s: no pending details", entry_id)
            self.update(entry_id, lambda current: current.__setitem__("state", "stale"), lease=lease)
            return
        event = {
            **entry,
            "scheduled_at": pending["scheduled_at"],
            "fired_at": pending["fired_at"],
        }
        failed = []
        for connection in pending["slots"]:
            if self.get(entry_id).state != "pending":
                return
            verify_lease(self.state, lease)
            if connection["id"] in pending["delivered"]:
                continue
            slot = dict(connection)
            connection_id = slot.pop("id")
            slot.pop("signal")
            try:
                restore(slot).deliver(event, f"{pending['id']}:{connection_id}", self.state)
            except Exception as exc:
                failed.append(connection_id)
                _log.warning("scheduler slot delivery failed: entry %s slot %s error %r", entry_id, slot, exc)
                continue

            def acknowledge(current):
                if current["state"] == "pending":
                    current["pending"]["delivered"].append(connection_id)

            self.update(entry_id, acknowledge, lease=lease)
        if not failed:
            self.update(entry_id, lambda current: self._finish(current, now), lease=lease)

    @staticmethod
    def _finish(entry: dict, now: float) -> None:
        if entry["state"] != "pending":
            return
        entry["state"] = "fired"
        if entry["kind"] == "cron":
            entry["state"] = "active"
            entry["due"] = next_due(entry["expression"], entry["timezone"], now)
        elif entry["kind"] == "interval":
            entry["state"] = "active"
            entry["due"] = now + entry["delay"]
        entry.pop("pending")

    def retire(self, entry_id: str) -> None:
        self.get(entry_id).cancel()
        self.change(lambda document: document["retired"].__setitem__(entry_id, True))

    def _reap(self, lease: Lease) -> None:
        def reap(document):
            for entry_id in document["retired"]:
                document["entries"].pop(entry_id, None)
            document["retired"] = {}

        self.change(reap, lease=lease)

    def run_due(self, *, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with dispatch_lock(self.state, self.name("entries")) as lease:
            fired = self.change(lambda document: self._claim(document, now), lease=lease)
            for entry_id, entry in self.document()["entries"].items():
                if entry["state"] == "pending":
                    self._deliver(entry_id, now, lease)
            self._reap(lease)
            return fired
