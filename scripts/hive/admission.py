"""Admit each launch by the measured headroom of the machine it lands on; a pending reservation counts against the room
for fifteen minutes until the controller collects the hive's answer to its launch, live, failed or ambiguous; the CPU
used and pod load readings carry a launched agent's own load from the next heartbeat on, while the five minute load a
hive without a CPU used reading falls back to takes up to five minutes to show it."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from scripts.swarm.keyspace import ROOT

if TYPE_CHECKING:
    from redis import Redis

CPU, RAM, QUOTA, PLACEMENT, NODE_CAP, MISSING = "cpu", "ram", "quota", "placement", "node cap", "missing telemetry"
MAX_AGENTS, DISK = "max agents", "disk"
HELD = "held"
HIVE_ADMISSION = "hive admission"
REASONS = (CPU, RAM, DISK, QUOTA, PLACEMENT, NODE_CAP, MAX_AGENTS, MISSING)
HOST_PERCENT = 80
SWARM_PERCENT = 40
AGENT_CPU = 0.5
AGENT_MB = 850
PENDING_MS = 900_000
NO_READING = "no fresh headroom reading"
# cpus/load5 shipped with the first headroom reading; a hive daemon still on an older image before total_mb
# joined the schema still publishes them, so a reading missing only the RAM fields is read, not refused.
CORE_FIELDS = ("cpus", "load5")
RAM_FIELDS = frozenset({"available_mb", "total_mb"})
POD_RAM_FIELDS = frozenset({"pod_available_mb", "pod_total_mb"})
POD_CPU_FIELDS = frozenset({"pod_cpus", "pod_load"})
LINE = f"under the {HOST_PERCENT} percent line"
QUOTA_MARKS = ("account has room", "account reservation is full")

_RESERVE = """
local now = redis.call('TIME')
local millis = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', millis)
if redis.call('ZSCORE', KEYS[1], ARGV[1]) then return {1, redis.call('ZCARD', KEYS[1])} end
local held = redis.call('ZCARD', KEYS[1])
if held >= tonumber(ARGV[2]) then return {0, held} end
redis.call('ZADD', KEYS[1], millis + tonumber(ARGV[3]), ARGV[1])
redis.call('PEXPIRE', KEYS[1], ARGV[3])
return {1, held}
"""

_PENDING = """
local now = redis.call('TIME')
local millis = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
return redis.call('ZCOUNT', KEYS[1], '(' .. millis, '+inf')
"""

_RECORD = """
local now = redis.call('TIME')
local millis = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
redis.call('ZADD', KEYS[1], millis + tonumber(ARGV[2]), ARGV[1])
redis.call('PEXPIRE', KEYS[1], ARGV[2])
"""
_ADMITTED = """
redis.call('HINCRBY', KEYS[2], 'admitted', 1)
"""


@dataclass(frozen=True)
class Room:
    room: int
    reason: str
    detail: str


class Held(RuntimeError):
    """The hub kept an offer pending because the asking hive is at its max agents; another hive may take it."""


def _host(measured: dict[str, Any], pending: int) -> list[tuple[int, str, str]]:
    cpus, load5 = measured["cpus"], measured["load5"]
    held = _held(pending)
    limits = [_host_cpu(cpus, load5, measured.get("used_cpus"), pending)]
    if all(field in measured for field in RAM_FIELDS):
        available, total = measured["available_mb"], measured["total_mb"]
        limits.append(
            (
                _ram_room(available, total, pending),
                RAM,
                f"{available} of {total} MB available, {AGENT_MB} MB per agent{held}, fits {{}} more {LINE}",
            )
        )
    return limits


def _host_cpu(cpus: float, load5: float, used: float | None, pending: int) -> tuple[int, str, str]:
    """The node CPU line: CPU used when the hive reads it, the five minute load only as a fallback and a guard that
    binds while used CPU is at or above the swarm share."""
    held = _held(pending)
    loaded = f"five minute load {load5:.2f} on {cpus} CPUs"
    if used is None:
        return (
            _cpu_room(cpus, load5, pending),
            CPU,
            f"{loaded}, {AGENT_CPU:g} CPU per agent{held}, fits {{}} more {LINE}",
        )
    shown = f"{used:.2f} of {cpus} CPUs used, {loaded}"
    if 100 * load5 > HOST_PERCENT * cpus and 100 * used >= SWARM_PERCENT * cpus:
        return 0, CPU, f"{shown} with CPU used at or above the {SWARM_PERCENT} percent swarm share, fits {{}} more"
    return _cpu_room(cpus, used, pending), CPU, f"{shown}, {AGENT_CPU:g} CPU per agent{held}, fits {{}} more {LINE}"


def _held(pending: int) -> str:
    return f" with {pending} pending launches" if pending else ""


def _cpu_room(cpus: float, load: float, pending: int) -> int:
    return int((HOST_PERCENT * cpus - 100 * load) // (100 * AGENT_CPU)) - pending


def _ram_room(available: int, total: int, pending: int) -> int:
    return (available * 100 - (100 - HOST_PERCENT) * total) // (100 * AGENT_MB) - pending


def _pod(measured: dict, pending: int) -> list[tuple[int, str, str]]:
    held = _held(pending)
    limits = []
    if all(field in measured for field in POD_CPU_FIELDS):
        cpus, load = measured["pod_cpus"], measured["pod_load"]
        limits.append(
            (
                _cpu_room(cpus, load, pending),
                CPU,
                f"pod load {load:.2f} on a {cpus:g} CPU limit, {AGENT_CPU:g} CPU per agent{held}, "
                f"fits {{}} more {LINE}",
            )
        )
    if all(field in measured for field in POD_RAM_FIELDS):
        available, total = measured["pod_available_mb"], measured["pod_total_mb"]
        limits.append(
            (
                _ram_room(available, total, pending),
                RAM,
                f"pod {available} of {total} MB available, {AGENT_MB} MB per agent{held}, fits {{}} more {LINE}",
            )
        )
    return limits


def _first(measured: dict, pending: int) -> list[tuple[int, str, str]]:
    held = _held(pending)
    limits = []
    for free, size, prefix in (("available_mb", "total_mb", ""), ("pod_available_mb", "pod_total_mb", "pod ")):
        if all(name in measured for name in (free, size)):
            available, total = measured[free], measured[size]
            used = 100 - available * 100 // total
            shown = f"{prefix}{available} of {total} MB available, {used} percent used"
            if available * 100 <= (100 - HOST_PERCENT) * total:
                limits.append((0, RAM, f"{shown}, at or above the {HOST_PERCENT} percent line"))
            else:
                fits = max(1, _ram_room(available, total, 0)) - pending
                limits.append((fits, RAM, f"{shown}, {AGENT_MB} MB per agent{held}, fits {{}} more {LINE}"))
    return limits or [(0, MISSING, "no memory reading")]


def _swarm(measured: dict, agents: int, max_agents: int | None) -> list[tuple[int, str, str]]:
    cpus, total = measured["cpus"], measured["total_mb"]
    cpu = int(SWARM_PERCENT * cpus / (100 * AGENT_CPU)) - agents
    ram = SWARM_PERCENT * total // (100 * AGENT_MB) - agents
    share = f"under the {SWARM_PERCENT} percent swarm share"
    limits = [
        (cpu, CPU, f"{agents} swarm agents at {AGENT_CPU:g} CPU each on {cpus} CPUs, fits {{}} more {share}"),
        (ram, RAM, f"{agents} swarm agents at {AGENT_MB} MB each in {total} MB, fits {{}} more {share}"),
    ]
    if max_agents is None:
        return limits
    return [limit for limit in limits if limit[0] >= max_agents - agents]


def room(
    measured: dict | None,
    swarm_agents: int | None = None,
    pending: int = 0,
    max_agents: int | None = None,
    first: bool = False,
) -> Room:
    if measured is None or any(field not in measured for field in CORE_FIELDS):
        return Room(0, MISSING, NO_READING)
    free, floor = measured.get("disk_free_gb"), measured.get("disk_floor_gb")
    if free is not None and floor is not None and free < floor:
        return Room(0, DISK, f"{free} GB free on the agent home, below the {floor} GB worktree floor")
    limits = _host(measured, pending) + _pod(measured, pending)
    if first:
        limits = _first(measured, pending) + [limit for limit in limits if limit[1] == CPU]
    if swarm_agents is not None and all(field in measured for field in RAM_FIELDS):
        limits += _swarm(measured, swarm_agents, max_agents)
    count, reason, detail = min(limits, key=lambda limit: limit[0])
    count = max(0, count)
    return Room(count, reason, detail.format(count))


def capped(found: Room, agents: int, max_agents: int) -> Room:
    left = max(0, max_agents - agents)
    if left >= found.room:
        return found
    return Room(left, MAX_AGENTS, f"{agents} of {max_agents} agents on the hive, fits {left} more")


def refusal(found: Room) -> str:
    return "" if found.room else f"{found.reason}: {found.detail}"


def key(machine: str) -> str:
    return f"{ROOT}:admission:{machine}"


def reserve(redis: "Redis", machine: str, member: str, limit: int, ttl_ms: int = PENDING_MS) -> tuple[bool, int]:
    admitted, held = redis.eval(_RESERVE, 1, key(machine), member, limit, ttl_ms)
    return bool(admitted), int(held)


def record(redis: "Redis", machine: str, member: str, totals: str = "") -> None:
    """Hold a pending workstation launch; with a totals hash, count it admitted in the same call."""
    if totals:
        redis.eval(_RECORD + _ADMITTED, 2, key(machine), totals, member, PENDING_MS)
    else:
        redis.eval(_RECORD, 1, key(machine), member, PENDING_MS)


def pending(redis: "Redis", machine: str) -> int:
    return int(redis.eval(_PENDING, 1, key(machine)))


def release(redis: "Redis", machine: str, member: str) -> None:
    redis.zrem(key(machine), member)


def category(reason: str) -> str:
    for found in (CPU, RAM, DISK, MAX_AGENTS, MISSING):
        if f"{found}: " in reason:
            return found
    if NODE_CAP in reason:
        return NODE_CAP
    if any(mark in reason for mark in QUOTA_MARKS) or QUOTA in reason.replace(":", " ").split():
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


def hive_lines(hives: list[dict[str, Any]]) -> list[str]:
    """One line per hive naming its admission room and the limit that binds it."""
    return [f"hive {hive['id']}  admission room {hive['room']}  {hive['limit']}" for hive in hives]


def binding(hives: list[dict[str, Any]], others: dict[str, int | None]) -> str:
    """The limit that binds spawning: the smallest of the summed hive admission room and the other known rooms, hive
    admission first on a tie; at zero hive room it names each hive's refusing limit."""
    room = sum(hive["room"] for hive in hives)
    limits = {HIVE_ADMISSION: room, **{name: count for name, count in others.items() if count is not None}}
    name = min(limits, key=limits.__getitem__)
    line = f"binding limit {name}, room {limits[name]}"
    if name != HIVE_ADMISSION or room:
        return line
    return line + ": " + "; ".join(f"hive {hive['id']} {hive['refusal']}" for hive in hives)


def view_lines(hives: list[dict[str, Any]], others: dict[str, int | None]) -> list[str]:
    """The hive admission lines and the binding limit line, none when no hive is live."""
    return [*hive_lines(hives), binding(hives, others)] if hives else []
