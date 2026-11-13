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


ENQUEUE = """
if redis.call('HSETNX', KEYS[1], 'head', ARGV[2]) == 0 then return 0 end
for index = 3, #ARGV, 2 do redis.call('HSET', KEYS[1], ARGV[index], ARGV[index + 1]) end
redis.call('ZADD', KEYS[2], redis.call('INCR', KEYS[3]), ARGV[1])
redis.call('SADD', KEYS[4], ARGV[1])
return 1
"""

DROP = """
if redis.call('ZREM', KEYS[2], ARGV[1]) == 0 then return 0 end
redis.call('DEL', KEYS[1])
redis.call('SREM', KEYS[3], ARGV[1])
return 1
"""

CLAIM = """
if redis.call('ZREM', KEYS[2], ARGV[1]) == 0 then return 0 end
redis.call('HSET', KEYS[1], 'state', 'leased', 'worker', ARGV[2], 'token', ARGV[3])
redis.call('ZADD', KEYS[3], ARGV[4], ARGV[1])
redis.call('SMOVE', KEYS[4], KEYS[5], ARGV[1])
return 1
"""

EXTEND = """
if ARGV[2] == '' or redis.call('HGET', KEYS[1], 'token') ~= ARGV[2] then return 0 end
if not redis.call('ZSCORE', KEYS[2], ARGV[1]) then return 0 end
redis.call('ZADD', KEYS[2], ARGV[3], ARGV[1])
return 1
"""

FINISH = """
if ARGV[2] == '' or redis.call('HGET', KEYS[1], 'token') ~= ARGV[2] then return 0 end
if redis.call('ZREM', KEYS[2], ARGV[1]) == 0 then return 0 end
redis.call('HSET', KEYS[1], 'state', 'done', 'outcome', ARGV[3], 'token', '')
redis.call('SADD', KEYS[3], ARGV[1])
redis.call('SMOVE', KEYS[4], KEYS[5], ARGV[1])
return 1
"""

REAP = """
local expires = redis.call('ZSCORE', KEYS[2], ARGV[1])
if not expires or tonumber(expires) > tonumber(ARGV[2]) then return '' end
local attempts = tonumber(redis.call('HGET', KEYS[1], 'attempts') or 0)
redis.call('ZREM', KEYS[2], ARGV[1])
if attempts < tonumber(ARGV[3]) then
  redis.call('HINCRBY', KEYS[1], 'attempts', 1)
  redis.call('HSET', KEYS[1], 'state', 'pending', 'worker', '', 'token', '')
  redis.call('ZADD', KEYS[3], redis.call('INCR', KEYS[4]), ARGV[1])
  redis.call('SMOVE', KEYS[6], KEYS[7], ARGV[1])
  return 'pending'
end
redis.call('HSET', KEYS[1], 'state', 'lost', 'outcome', 'lost', 'token', '')
redis.call('SADD', KEYS[5], ARGV[1])
redis.call('SMOVE', KEYS[6], KEYS[8], ARGV[1])
return 'lost'
"""


def _key(*parts: str) -> str:
    return PREFIX + ":".join(parts)


def _job(job: str) -> str:
    return _key("job", job)


def _head(head: str, state: str) -> str:
    return _key("head", head, state)


class Queue:
    """Job records and their state sets; every transition is one Lua script, so a crash never strands a job."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self._enqueue = client.register_script(ENQUEUE)
        self._drop = client.register_script(DROP)
        self._claim = client.register_script(CLAIM)
        self._extend = client.register_script(EXTEND)
        self._finish = client.register_script(FINISH)
        self._reap = client.register_script(REAP)

    def enqueue(self, units: Sequence[Unit], head: str, requirements: Mapping[str, Requirement]) -> list[str]:
        """Add each unit of the head as a pending job unless that job is already known."""
        added: list[str] = []
        for unit in units:
            job = f"{head}:{unit.kind}:{unit.key}"
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
            fields = [item for pair in record.items() for item in pair]
            keys = [_job(job), _key("pending"), _key("seq"), _head(head, "pending")]
            if self._enqueue(keys=keys, args=[job, head, *fields]):
                added.append(job)
        return added

    def supersede(self, head: str) -> list[str]:
        """Remove the head's pending jobs; its leased jobs run to completion."""
        mine = self.members(head, "pending")
        keys = [_key("pending"), _head(head, "pending")]
        return [job for job in self.pending() if job in mine and self._drop(keys=[_job(job), *keys], args=[job])]

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

    def claim(self, job: Job, worker: str, token: str, expires_ms: int) -> bool:
        """Lease a pending job to the worker; only one caller ever wins it."""
        keys = [_job(job.id), _key("pending"), _key("leased"), _head(job.head, "pending"), _head(job.head, "leased")]
        return bool(self._claim(keys=keys, args=[job.id, worker, token, expires_ms]))

    def extend(self, job: str, token: str, expires_ms: int) -> bool:
        """Move the expiry of a lease the token holds."""
        return bool(self._extend(keys=[_job(job), _key("leased")], args=[job, token, expires_ms]))

    def finish(self, job: Job, token: str, outcome: str) -> bool:
        """Record the outcome of a lease the token holds; only one of finishing and reaping wins it."""
        keys = [_job(job.id), _key("leased"), _key("done"), _head(job.head, "leased"), _head(job.head, "done")]
        return bool(self._finish(keys=keys, args=[job.id, token, outcome]))

    def expired(self, now_ms: int) -> list[str]:
        return list(self.client.zrangebyscore(_key("leased"), "-inf", now_ms))

    def reap(self, job: Job, now_ms: int, requeues: int) -> str:
        """Requeue an expired lease while attempts remain, else mark it lost; empty when it no longer expired."""
        keys = [
            _job(job.id),
            _key("leased"),
            _key("pending"),
            _key("seq"),
            _key("done"),
            _head(job.head, "leased"),
            _head(job.head, "pending"),
            _head(job.head, "done"),
        ]
        return str(self._reap(keys=keys, args=[job.id, now_ms, requeues]))
