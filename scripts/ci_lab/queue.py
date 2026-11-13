"""Lab queue state in Redis: pending, leased and done jobs, globally and per head."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from scripts.ci_lab.units import Unit

__all__ = ["PREFIX", "Job", "Queue", "Requirement"]

PREFIX = "agentihooks-ci:queue:"


@dataclass(frozen=True)
class Requirement:
    slots: int = 1
    capabilities: frozenset[str] = frozenset()


DEFAULT = Requirement()


@dataclass(frozen=True)
class Job:
    id: str
    head: str
    unit: Unit
    requirement: Requirement
    state: str
    attempts: int
    worker: str
    outcome: str


def _key(*parts: str) -> str:
    return PREFIX + ":".join(parts)


def _job(job: str) -> str:
    return _key("job", job)


def _head(head: str, state: str) -> str:
    return _key("head", head, state)


class Queue:
    """Job records and their state sets; a removal from a pending or leased set decides every race."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def enqueue(self, units: Sequence[Unit], head: str, requirements: Mapping[str, Requirement]) -> list[str]:
        """Add each unit of the head as a pending job unless that job is already known."""
        added: list[str] = []
        for unit in units:
            job = f"{head}:{unit.kind}:{unit.key}"
            if not self.client.hsetnx(_job(job), "head", head):
                continue
            need = requirements.get(unit.key, DEFAULT)
            record = {
                "kind": unit.kind,
                "key": unit.key,
                "members": json.dumps(unit.members),
                "slots": need.slots,
                "capabilities": json.dumps(sorted(need.capabilities)),
                "state": "pending",
                "attempts": 0,
                "worker": "",
                "token": "",
                "outcome": "",
            }
            seq = self.client.incr(_key("seq"))
            pipe = self.client.pipeline()
            pipe.hset(_job(job), mapping=record)
            pipe.zadd(_key("pending"), {job: seq})
            pipe.sadd(_head(head, "pending"), job)
            pipe.execute()
            added.append(job)
        return added

    def supersede(self, head: str) -> list[str]:
        """Remove the head's pending jobs; its leased jobs run to completion."""
        mine = self.members(head, "pending")
        dropped: list[str] = []
        for job in self.pending():
            if job in mine and self.client.zrem(_key("pending"), job):
                pipe = self.client.pipeline()
                pipe.delete(_job(job))
                pipe.srem(_head(head, "pending"), job)
                pipe.execute()
                dropped.append(job)
        return dropped

    def pending(self) -> list[str]:
        return list(self.client.zrange(_key("pending"), 0, -1))

    def members(self, head: str, state: str) -> set[str]:
        return set(self.client.smembers(_head(head, state)))

    def job(self, job: str) -> Job | None:
        record = self.client.hgetall(_job(job))
        if not record:
            return None
        return Job(
            id=job,
            head=record["head"],
            unit=Unit(kind=record["kind"], key=record["key"], members=tuple(json.loads(record["members"]))),
            requirement=Requirement(
                slots=int(record["slots"]), capabilities=frozenset(json.loads(record["capabilities"]))
            ),
            state=record["state"],
            attempts=int(record["attempts"]),
            worker=record["worker"],
            outcome=record["outcome"],
        )

    def token(self, job: str) -> str:
        return str(self.client.hget(_job(job), "token") or "")

    def attempts(self, job: str) -> int:
        return int(self.client.hget(_job(job), "attempts") or 0)

    def claim(self, job: str) -> bool:
        """Take a job out of pending; only one caller ever wins it."""
        return bool(self.client.zrem(_key("pending"), job))

    def lease(self, job: str, worker: str, token: str, expires_ms: int) -> None:
        head = self.client.hget(_job(job), "head")
        pipe = self.client.pipeline()
        pipe.hset(_job(job), mapping={"state": "leased", "worker": worker, "token": token})
        pipe.zadd(_key("leased"), {job: expires_ms})
        pipe.smove(_head(head, "pending"), _head(head, "leased"), job)
        pipe.execute()

    def extend(self, job: str, expires_ms: int) -> bool:
        if self.client.zscore(_key("leased"), job) is None:
            return False
        self.client.zadd(_key("leased"), {job: expires_ms}, xx=True)
        return True

    def release(self, job: str) -> bool:
        """Take a job out of leased; only one of completion and reaping ever wins it."""
        return bool(self.client.zrem(_key("leased"), job))

    def expired(self, now_ms: int) -> list[str]:
        return list(self.client.zrangebyscore(_key("leased"), "-inf", now_ms))

    def requeue(self, job: str) -> None:
        head = self.client.hget(_job(job), "head")
        seq = self.client.incr(_key("seq"))
        pipe = self.client.pipeline()
        pipe.hincrby(_job(job), "attempts", 1)
        pipe.hset(_job(job), mapping={"state": "pending", "worker": "", "token": ""})
        pipe.zadd(_key("pending"), {job: seq})
        pipe.smove(_head(head, "leased"), _head(head, "pending"), job)
        pipe.execute()

    def finish(self, job: str, state: str, outcome: str) -> None:
        head = self.client.hget(_job(job), "head")
        pipe = self.client.pipeline()
        pipe.hset(_job(job), mapping={"state": state, "outcome": outcome, "token": ""})
        pipe.sadd(_key("done"), job)
        pipe.smove(_head(head, "leased"), _head(head, "done"), job)
        pipe.execute()
