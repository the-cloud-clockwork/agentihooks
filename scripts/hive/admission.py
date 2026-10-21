"""Admit each launch by the measured headroom of the machine it lands on; pending reservations count against the room
for three five minute load windows, until that load shows about 95 percent of the launched agent."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from scripts.swarm.host_budget import MEMORY_PER_AGENT_MB

if TYPE_CHECKING:
    from redis import Redis

CPU, RAM, QUOTA, PLACEMENT, NODE_CAP, MISSING = "cpu", "ram", "quota", "placement", "node cap", "missing telemetry"
REASONS = (CPU, RAM, QUOTA, PLACEMENT, NODE_CAP, MISSING)
HOST_PERCENT = 80
SWARM_PERCENT = 40
AGENT_CPU = 0.5
AGENT_MB = MEMORY_PER_AGENT_MB
PENDING_MS = 900_000
NO_READING = "no fresh headroom reading"
FIELDS = ("cpus", "load5", "available_mb", "total_mb")
QUOTA_MARKS = ("account has room", "account reservation is full", "quota")

_RESERVE = """
local now = redis.call('TIME')
local millis = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', millis)
local held = redis.call('ZCARD', KEYS[1])
if redis.call('ZSCORE', KEYS[1], ARGV[1]) then return {1, held} end
if held >= tonumber(ARGV[2]) then return {0, held} end
redis.call('ZADD', KEYS[1], millis + tonumber(ARGV[3]), ARGV[1])
redis.call('PEXPIRE', KEYS[1], ARGV[3])
return {1, held}
"""


@dataclass(frozen=True)
class Room:
    room: int
    reason: str
    detail: str


def _host(measured: dict, pending: int) -> list[tuple[int, str, str]]:
    cpus, load5, available, total = (measured[field] for field in FIELDS)
    cpu = int((HOST_PERCENT * cpus - 100 * load5) // (100 * AGENT_CPU)) - pending
    ram = (available * 100 - (100 - HOST_PERCENT) * total) // (100 * AGENT_MB) - pending
    line = f"under the {HOST_PERCENT} percent line"
    held = f" with {pending} pending launches" if pending else ""
    return [
        (
            cpu,
            CPU,
            f"five minute load {load5:.2f} on {cpus} CPUs, {AGENT_CPU:g} CPU per agent{held}, fits {{}} more {line}",
        ),
        (ram, RAM, f"{available} of {total} MB available, {AGENT_MB} MB per agent{held}, fits {{}} more {line}"),
    ]


def _swarm(measured: dict, agents: int) -> list[tuple[int, str, str]]:
    cpus, total = measured["cpus"], measured["total_mb"]
    cpu = int(SWARM_PERCENT * cpus // (100 * AGENT_CPU)) - agents
    ram = SWARM_PERCENT * total // (100 * AGENT_MB) - agents
    share = f"under the {SWARM_PERCENT} percent swarm share"
    return [
        (cpu, CPU, f"{agents} swarm agents at {AGENT_CPU:g} CPU each on {cpus} CPUs, fits {{}} more {share}"),
        (ram, RAM, f"{agents} swarm agents at {AGENT_MB} MB each in {total} MB, fits {{}} more {share}"),
    ]


def room(measured: dict | None, swarm_agents: int | None = None, pending: int = 0) -> Room:
    if measured is None or any(field not in measured for field in FIELDS):
        return Room(0, MISSING, NO_READING)
    limits = _host(measured, pending) + ([] if swarm_agents is None else _swarm(measured, swarm_agents))
    count, reason, detail = min(limits, key=lambda limit: limit[0])
    count = max(0, count)
    return Room(count, reason, detail.format(count))


def refusal(found: Room, pending: int) -> str:
    return "" if pending < found.room else f"{found.reason}: {found.detail}"


def key(machine: str) -> str:
    from scripts.swarm.keyspace import ROOT

    return f"{ROOT}:admission:{machine}"


def reserve(redis: "Redis", machine: str, member: str, limit: int, ttl_ms: int = PENDING_MS) -> tuple[bool, int]:
    if type(ttl_ms) is not int or not 0 < ttl_ms <= PENDING_MS:
        raise ValueError("invalid admission lifetime")
    admitted, held = redis.eval(_RESERVE, 1, key(machine), member, limit, ttl_ms)
    return bool(admitted), int(held)


def pending(redis: "Redis", machine: str) -> int:
    seconds, micros = redis.time()
    return redis.zcount(key(machine), f"({int(seconds) * 1000 + int(micros) // 1000}", "+inf")


def release(redis: "Redis", machine: str, member: str) -> None:
    redis.zrem(key(machine), member)


def category(reason: str) -> str:
    for found in (CPU, RAM, MISSING):
        if f"{found}: " in reason:
            return found
    if NODE_CAP in reason:
        return NODE_CAP
    if any(mark in reason for mark in QUOTA_MARKS):
        return QUOTA
    return PLACEMENT


def counts(waits: dict) -> dict:
    found = dict.fromkeys(REASONS, 0)
    for row in waits.values():
        found[category(row["reason"])] += 1
    return {reason: count for reason, count in found.items() if count}


def status_lines(waits: dict) -> list[str]:
    shown = "  ".join(f"{reason} {count}" for reason, count in counts(waits).items())
    return [f"admission refusals  {shown}"] if shown else []
