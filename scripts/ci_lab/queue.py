"""Lab queue state in Redis: pending, leased and done jobs, globally and per head."""

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from scripts.ci_lab.delivery import CLASSES
from scripts.ci_lab.outcomes import LOST
from scripts.ci_lab.units import Unit

__all__ = ["BAND", "PAGE", "PREFIX", "RANKS", "UNRANKED", "Job", "Queue", "Requirement"]

PREFIX = "agentihooks-ci:queue:"
RANKS = len(CLASSES)
UNRANKED = RANKS
BAND = 2**40
PAGE = 64


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
    base: str = ""
    grader: str = ""
    leased_ms: int = 0
    rank: int = UNRANKED


SCORE = f"""
local function rank_of(job) return tonumber(redis.call('HGET', job, 'rank') or {RANKS}) end
local function score(counter, rank) return counter - ({RANKS} - rank) * {BAND} end
"""

ENQUEUE = (
    SCORE
    + """
local rank = tonumber(ARGV[3])
if redis.call('HSETNX', KEYS[1], 'head', ARGV[2]) == 0 then
  local placed = redis.call('ZSCORE', KEYS[2], ARGV[1])
  local held = rank_of(KEYS[1])
  if placed and rank < held then
    redis.call('HSET', KEYS[1], 'rank', rank)
    redis.call('ZADD', KEYS[2], score(tonumber(placed), rank) - score(0, held), ARGV[1])
  end
  return 0
end
for index = 4, #ARGV, 2 do redis.call('HSET', KEYS[1], ARGV[index], ARGV[index + 1]) end
redis.call('ZADD', KEYS[2], score(redis.call('INCR', KEYS[3]), rank), ARGV[1])
redis.call('SADD', KEYS[4], ARGV[1])
return 1
"""
)

DROP = """
if redis.call('ZREM', KEYS[2], ARGV[1]) == 0 then return 0 end
redis.call('DEL', KEYS[1])
redis.call('SREM', KEYS[3], ARGV[1])
return 1
"""

CLAIM = """
if redis.call('ZREM', KEYS[2], ARGV[1]) == 0 then return 0 end
redis.call('HSET', KEYS[1], 'state', 'leased', 'worker', ARGV[2], 'token', ARGV[3], 'leased_ms', ARGV[5])
redis.call('ZADD', KEYS[3], ARGV[4], ARGV[1])
redis.call('SMOVE', KEYS[4], KEYS[5], ARGV[1])
return 1
"""

TALLY = """
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
return redis.call('HSETNX', KEYS[1], 'tallied:' .. ARGV[1], 1)
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

REAP = (
    SCORE
    + """
local expires = redis.call('ZSCORE', KEYS[2], ARGV[1])
if not expires or tonumber(expires) > tonumber(ARGV[2]) then return '' end
local attempts = tonumber(redis.call('HGET', KEYS[1], 'attempts') or 0)
redis.call('ZREM', KEYS[2], ARGV[1])
if attempts < tonumber(ARGV[3]) then
  redis.call('HINCRBY', KEYS[1], 'attempts', 1)
  redis.call('HSET', KEYS[1], 'state', 'pending', 'worker', '', 'token', '')
  redis.call('ZADD', KEYS[3], score(redis.call('INCR', KEYS[4]), rank_of(KEYS[1])), ARGV[1])
  redis.call('SMOVE', KEYS[6], KEYS[7], ARGV[1])
  return 'pending'
end
redis.call('HSET', KEYS[1], 'state', 'lost', 'outcome', ARGV[4], 'token', '')
redis.call('SADD', KEYS[5], ARGV[1])
redis.call('SMOVE', KEYS[6], KEYS[8], ARGV[1])
return 'lost'
"""
)

RETRY = (
    SCORE
    + """
if redis.call('HGET', KEYS[1], 'state') ~= 'done' then return 0 end
redis.call('HINCRBY', KEYS[1], 'attempts', 1)
redis.call('HSET', KEYS[1], 'state', 'pending', 'worker', '', 'outcome', '')
redis.call('SREM', KEYS[2], ARGV[1])
redis.call('ZADD', KEYS[3], score(redis.call('INCR', KEYS[4]), rank_of(KEYS[1])), ARGV[1])
redis.call('SMOVE', KEYS[5], KEYS[6], ARGV[1])
return 1
"""
)

PRUNE = """
local fields = redis.call('HGETALL', KEYS[1])
local dropped = 0
for index = 1, #fields, 2 do
  if tonumber(string.match(fields[index + 1], '(%-?%d+)$')) < tonumber(ARGV[1]) then
    redis.call('HDEL', KEYS[1], fields[index])
    dropped = dropped + 1
  end
end
return dropped
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
        self._retry = client.register_script(RETRY)
        self._prune = client.register_script(PRUNE)
        self._tally = client.register_script(TALLY)

    def enqueue(
        self,
        units: Sequence[Unit],
        head: str,
        requirements: Mapping[str, Requirement],
        base: str = "",
        grader: str = "",
        rank: int = UNRANKED,
    ) -> list[str]:
        """Add each unit of the head as a pending job, with the base and grader revision its grade reads, unless that
        job is already known. The pending order is the rank first, the most urgent class lowest, then the order of
        enqueue; a job with no rank sorts after every ranked one, as every job enqueued before ranks existed does. A
        known job still pending under a less urgent rank moves to this one and keeps its place by enqueue order."""
        added: list[str] = []
        for unit in units:
            job = f"{head}:{unit.kind}:{unit.key}"
            need = requirements.get(unit.key, DEFAULT)
            record = {
                "base": base,
                "grader": grader,
                "kind": unit.kind,
                "key": unit.key,
                "members": json.dumps(unit.members),
                "slots": need.slots,
                "capabilities": json.dumps(sorted(need.capabilities)),
                "state": "pending",
                "attempts": 0,
                "worker": "",
                "outcome": "",
                "rank": rank,
            }
            fields = [item for pair in record.items() for item in pair]
            keys = [_job(job), _key("pending"), _key("seq"), _head(head, "pending")]
            if self._enqueue(keys=keys, args=[job, head, rank, *fields]):
                added.append(job)
        return added

    def supersede(self, head: str) -> list[str]:
        """Remove the head's pending jobs; its leased jobs run to completion."""
        mine = self.members(head, "pending")
        keys = [_key("pending"), _head(head, "pending")]
        return [job for job in self.pending() if job in mine and self._drop(keys=[_job(job), *keys], args=[job])]

    def pending(self) -> list[str]:
        return list(self.client.zrange(_key("pending"), 0, -1))

    def ordered(self, page: int = PAGE) -> Iterator[str]:
        """Yield the pending jobs in lease order, read page jobs at a time from the score after the last job read, so
        a job that leaves the pending set between two pages shifts none of the rest."""
        after = "-inf"
        while True:
            rows = self.client.zrangebyscore(_key("pending"), after, "+inf", start=0, num=page, withscores=True)
            for job, _ in rows:
                yield job
            if len(rows) < page:
                return
            after = f"({rows[-1][1]}"

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
            base=record["base"],
            grader=record["grader"],
            leased_ms=int(record.get("leased_ms", 0)),
            rank=int(record.get("rank", UNRANKED)),
        )

    def slots(self, jobs: Sequence[str]) -> dict[str, tuple[int, int]]:
        """Return the slots and lease start of each job whose record still exists, read with HMGET in one pipeline that
        opens no transaction and builds no job; a job never leased starts at 0."""
        pipe = self.client.pipeline(transaction=False)
        for job in jobs:
            pipe.hmget(_job(job), "slots", "leased_ms")
        return {
            job: (int(slots), int(leased or 0))
            for job, (slots, leased) in zip(jobs, pipe.execute())
            if slots is not None
        }

    def token(self, job: str) -> str:
        return str(self.client.hget(_job(job), "token") or "")

    def claim(self, job: Job, worker: str, token: str, expires_ms: int, leased_ms: int) -> bool:
        """Lease a pending job to the worker from leased_ms; only one caller ever wins it."""
        keys = [_job(job.id), _key("pending"), _key("leased"), _head(job.head, "pending"), _head(job.head, "leased")]
        return bool(self._claim(keys=keys, args=[job.id, worker, token, expires_ms, leased_ms]))

    def tally(self, job: Job) -> bool:
        """Mark the job's finished attempt counted; True only for the first caller of that attempt."""
        return bool(self._tally(keys=[_job(job.id)], args=[job.attempts]))

    def extend(self, job: str, token: str, expires_ms: int) -> bool:
        """Move the expiry of a lease the token holds."""
        return bool(self._extend(keys=[_job(job), _key("leased")], args=[job, token, expires_ms]))

    def finish(self, job: Job, token: str, outcome: str) -> bool:
        """Record the outcome of a lease the token holds; only one of finishing and reaping wins it."""
        keys = [_job(job.id), _key("leased"), _key("done"), _head(job.head, "leased"), _head(job.head, "done")]
        return bool(self._finish(keys=keys, args=[job.id, token, outcome]))

    def expired(self, now_ms: int) -> list[str]:
        return list(self.client.zrangebyscore(_key("leased"), 0, now_ms))

    def leased(self) -> list[str]:
        """Return every leased job, soonest expiry first."""
        return list(self.client.zrange(_key("leased"), 0, -1))

    def holds(self, worker: str) -> bool:
        """Whether the worker holds a live lease on any job."""
        return any(self.client.hget(_job(job), "worker") == worker for job in self.leased())

    def offer(self, worker: str, free_slots: int, at_ms: int) -> None:
        """Record the free slots a worker offered on its latest lease poll and when."""
        self.client.hset(_key("offers"), worker, f"{free_slots} {at_ms}")

    def offers(self, since_ms: int) -> dict[str, tuple[int, int]]:
        """Return each worker's latest offered free slots and the millisecond it offered them, for offers made at or
        after since_ms."""
        offered: dict[str, tuple[int, int]] = {}
        for worker, raw in self.client.hgetall(_key("offers")).items():
            free_slots, at_ms = map(int, raw.split())
            if at_ms >= since_ms:
                offered[worker] = (free_slots, at_ms)
        return offered

    def prune(self, since_ms: int) -> int:
        """Forget every offer made before since_ms and return how many; one script reads and deletes, so an offer a
        worker renews meanwhile stays."""
        return int(self._prune(keys=[_key("offers")], args=[since_ms]))

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
        return str(self._reap(keys=keys, args=[job.id, now_ms, requeues, LOST]))

    def retry(self, job: Job) -> bool:
        """Move a finished job back to pending with one more attempt; False when it is not finished."""
        keys = [
            _job(job.id),
            _key("done"),
            _key("pending"),
            _key("seq"),
            _head(job.head, "done"),
            _head(job.head, "pending"),
        ]
        return bool(self._retry(keys=keys, args=[job.id]))
