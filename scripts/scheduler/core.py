import json
import logging
import os
import socket
import time
import uuid
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Callable

from hooks._redis import redis_key
from scripts.scheduler.lease import dispatch_lock, verify_lease
from scripts.scheduler.model import Schedule, next_due
from scripts.scheduler.slots import CallableSlot, Slot, restore

if TYPE_CHECKING:
    from redis import Redis
    from redis.lock import Lock

_log = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class Entry:
    scheduler: "Scheduler"
    id: str

    @property
    def record(self) -> dict:
        raw = self.scheduler.redis.hget(self.scheduler.key("entries"), self.id)
        if raw is None:
            raise KeyError(self.id)
        return json.loads(raw)

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
    def __init__(self, redis: "Redis", *, machine: str | None = None) -> None:
        self.redis = redis
        self.machine = machine or socket.gethostname()

    def key(self, suffix: str) -> str:
        from scripts.swarm_ledger import ledger_link

        if not ledger_link.local_mode(os.environ):
            from scripts.swarm.keyspace import ROOT

            if member := os.environ.get("AGENTIHOOKS_HIVE_ID"):
                return f"{ROOT}:hive:{member}:scheduler:{self.machine}:{suffix}"
            return f"{ROOT}:scheduler:{self.machine}:{suffix}"
        return redis_key("scheduler", f"{self.machine}:{suffix}")

    def get(self, entry_id: str) -> Entry:
        entry = Entry(self, entry_id)
        entry.record
        return entry

    def owned(self, owner: str) -> list[Entry]:
        entries = self.redis.hgetall(self.key("entries"))
        return [Entry(self, entry_id) for entry_id, raw in entries.items() if json.loads(raw)["owner"] == owner]

    def records(self) -> list[dict]:
        return [json.loads(raw) for raw in self.redis.hvals(self.key("entries"))]

    def update(self, entry_id: str, change: Callable[[dict], None], *, lease: "Lock | None" = None) -> None:
        from redis.exceptions import WatchError

        key = self.key("entries")
        while True:
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    if lease is not None:
                        pipe.watch(lease.name)
                        verify_lease(pipe, lease)
                    raw = pipe.hget(key, entry_id)
                    if raw is None:
                        raise KeyError(entry_id)
                    entry = json.loads(raw)
                    change(entry)
                    pipe.multi()
                    pipe.hset(key, entry_id, json.dumps(entry))
                    if entry["state"] == "active":
                        pipe.zadd(self.key("due"), {entry_id: entry["due"]})
                    else:
                        pipe.zrem(self.key("due"), entry_id)
                    if "pending" in entry:
                        pipe.sadd(self.key("pending"), entry_id)
                    else:
                        pipe.srem(self.key("pending"), entry_id)
                    pipe.execute()
                    return
                except WatchError:
                    continue

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
        self.redis.hset(self.key("entries"), entry_id, json.dumps(entry))
        return Entry(self, entry_id)

    def _claim(self, entry_id: str, now: float, lease: "Lock") -> dict | None:
        from redis.exceptions import WatchError

        key = self.key("entries")
        with self.redis.pipeline() as pipe:
            try:
                pipe.watch(key, lease.name)
                verify_lease(pipe, lease)
                entry = json.loads(pipe.hget(key, entry_id))
                if entry["state"] != "active" or entry["due"] > now:
                    return None
                entry["pending"] = {
                    "id": uuid.uuid4().hex,
                    "scheduled_at": entry["due"],
                    "fired_at": now,
                    "slots": [slot for slot in entry["slots"] if slot["signal"] == entry["signal"]],
                    "delivered": [],
                }
                entry["state"] = "pending"
                pipe.multi()
                pipe.hset(key, entry_id, json.dumps(entry))
                pipe.zrem(self.key("due"), entry_id)
                pipe.sadd(self.key("pending"), entry_id)
                pipe.execute()
                return entry
            except WatchError:
                verify_lease(self.redis, lease)
                return None

    def _deliver(self, entry_id: str, now: float, lease: "Lock") -> None:
        entry = self.get(entry_id).record
        pending = entry["pending"]
        event = {
            **entry,
            "scheduled_at": pending["scheduled_at"],
            "fired_at": pending["fired_at"],
        }
        failed = []
        for connection in pending["slots"]:
            if self.get(entry_id).state != "pending":
                return
            verify_lease(self.redis, lease)
            if connection["id"] in pending["delivered"]:
                continue
            slot = dict(connection)
            connection_id = slot.pop("id")
            slot.pop("signal")
            try:
                restore(slot).deliver(event, f"{pending['id']}:{connection_id}", self.redis)
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

    def retire(self, entry_id: str, wake_key: str) -> None:
        self.get(entry_id).cancel()
        self.redis.hset(self.key("retired"), entry_id, wake_key)

    def _reap(self, lease: "Lock") -> None:
        for entry_id, wake_key in self.redis.hgetall(self.key("retired")).items():
            verify_lease(self.redis, lease)
            with self.redis.pipeline() as pipe:
                pipe.hdel(self.key("entries"), entry_id)
                pipe.delete(wake_key)
                pipe.hdel(self.key("retired"), entry_id)
                pipe.execute()

    def run_due(self, *, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with dispatch_lock(self.redis, self.key("dispatch-lock")) as lease:
            fired = 0
            for entry_id in self.redis.zrangebyscore(self.key("due"), "-inf", now):
                if self._claim(entry_id, now, lease) is not None:
                    fired += 1
            for entry_id in self.redis.smembers(self.key("pending")):
                entry = self.get(entry_id).record
                if entry["state"] == "pending":
                    self._deliver(entry_id, now, lease)
            self._reap(lease)
            return fired
