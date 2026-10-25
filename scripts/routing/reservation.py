from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from hooks.context import account_sessions
from hooks.context.account_sessions import INTERACTIVE_PREFIX
from scripts import session_bands
from scripts.routing import master_account
from scripts.routing.slots import API

if TYPE_CHECKING:
    from scripts.swarm.store import RedisStore

CEILINGS = ((3, 50.0), (2, 60.0), (1, 70.0))
LOWEST = min(limit for _, limit in CEILINGS)
MASTER_LEAVES = 100.0 - session_bands.WEEK_FLOOR
MASTER_NAME = "master@"
AGENT_NAME = "AGENTIHOOKS_AGENT_NAME"


def ceiling(masters_live: int) -> float | None:
    return next((limit for count, limit in CEILINGS if masters_live >= count), None)


def reached(week_used: float | None, masters_live: int) -> bool:
    limit = ceiling(masters_live)
    return limit is not None and week_used is not None and week_used >= limit


def reason(masters_live: int) -> str:
    limit = ceiling(masters_live)
    if limit is None:
        return ""
    noun = "master" if masters_live == 1 else "masters"
    return f"reserved for {masters_live} live {noun}: workers stop at {limit:g}% of the week"


def _session(harness: str, pid: int, proc: Path) -> bool:
    if harness == "codex":
        return account_sessions._is_codex_session(pid, proc)
    return account_sessions._is_interactive_claude(pid, proc)


def interactive_sessions(harness: str, slug: str, proc: Path = account_sessions._PROC) -> int:
    marker = f"{INTERACTIVE_PREFIX}{slug}"
    try:
        pids = [int(entry.name) for entry in proc.iterdir() if entry.name.isdigit()]
    except OSError:
        return 0
    return sum(1 for pid in pids if _session(harness, pid, proc) and marker in account_sessions._env_names(pid, proc))


def master_agents(store: "RedisStore | None", harness: str, slug: str) -> int:
    from scripts.swarm.store import MASTER

    if store is None:
        return 0
    return sum(
        1
        for swarm in store.slugs()
        for agent in store.agents(swarm)
        if agent.lane == MASTER
        and agent.state != "finished"
        and agent.harness == harness
        and agent.account == slug
        and agent.route_kind not in ("interactive", "master-login")
    )


def masters_live(
    harness: str, slug: str, store: "RedisStore | None" = None, proc: Path = account_sessions._PROC
) -> int:
    return interactive_sessions(harness, slug, proc) + master_agents(store, harness, slug)


@dataclass(frozen=True)
class Reservation:
    harness: str
    slug: str
    masters: int

    @property
    def ceiling(self) -> float | None:
        return ceiling(self.masters)

    @property
    def reason(self) -> str:
        return reason(self.masters)

    def closes(self, slug: str, week_used: float | None) -> bool:
        return slug == self.slug and reached(week_used, self.masters)

    def closes_row(self, harness: str, kind: str, slug: str, week_left: float | None) -> bool:
        return harness == self.harness and kind != API and week_left is not None and self.closes(slug, 100 - week_left)


def _store(environ: Mapping[str, str]) -> "RedisStore | None":
    from redis.exceptions import RedisError

    from scripts.swarm.store import RedisStore, redis_client

    try:
        return RedisStore(redis_client(environ))
    except (RedisError, OSError):
        return None


def current(
    harness: str,
    environ: Mapping[str, str],
    store_of: "Callable[[Mapping[str, str]], RedisStore | None] | None" = None,
    proc: Path = account_sessions._PROC,
) -> Reservation | None:
    # redis is imported here: the quota hook loads this module on every tool call.
    from redis.exceptions import RedisError

    try:
        declared = master_account.load(environ).get(harness)
        if declared is None:
            return None
        store = (store_of or _store)(environ)
        from scripts.routing import aggregate

        if aggregate.enabled(environ):
            import time

            row = aggregate.current(time.time()).get(f"{harness}:{declared.slug}", {})
            masters = row.get("masters", 0) + master_agents(store, harness, declared.slug)
        else:
            masters = masters_live(harness, declared.slug, store, proc)
        return Reservation(harness, declared.slug, masters)
    except (OSError, RedisError):
        return None


def is_master_session(environ: Mapping[str, str]) -> bool:
    if any(name.startswith(INTERACTIVE_PREFIX) for name in environ):
        return True
    return AGENT_NAME in environ and environ[AGENT_NAME].startswith(MASTER_NAME)


def for_worker(harness: str, environ: Mapping[str, str]) -> Reservation | None:
    return None if is_master_session(environ) else current(harness, environ)


_LAUNCH = """
local mode, slot, execution = ARGV[1], ARGV[2], ARGV[3]
local raw = redis.call('GET', KEYS[2])
local held = raw and cjson.decode(raw) or nil
local now = redis.call('TIME')
local millis = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
local seen, used, previous = {}, 0, nil
for _, hive in ipairs(redis.call('SMEMBERS', KEYS[3])) do
    local key = ARGV[6] .. ':hive:' .. hive
    local heartbeat = tonumber(redis.call('HGET', key, 'heartbeat_at') or '0')
    local sessions = redis.call('GET', key .. ':sessions')
    if heartbeat > 0 and millis - heartbeat <= 90000 and sessions then
        local report = cjson.decode(sessions)
        used = used + (report[slot] or 0)
        for _, id in ipairs((report._executions or {})[slot] or {}) do
            seen[id] = true
        end
    end
end
if mode == 'release' then
    if not held or held.state == 'exited' then return {0, used} end
    held.state = 'exited'
    redis.call('SET', KEYS[2], cjson.encode(held), 'PX', 45000)
    return {1, used}
end
if mode == 'ack' then
    if held and held.state == 'exited' then return {0, used} end
    if held and held.state == 'live' then return {1, used} end
    if not held then
        if slot == '' then return {0, used} end
        held = {slot=slot}
        redis.call('SADD', KEYS[1], execution)
    end
    held.state = 'live'
    redis.call('SET', KEYS[2], cjson.encode(held), 'PX', ARGV[5])
    return {1, used}
end
if held then
    if held.slot ~= slot or held.state == 'exited' then return {0, used} end
    return {1, used}
end
for _, id in ipairs(redis.call('SMEMBERS', KEYS[1])) do
    local other = redis.call('GET', ARGV[6] .. ':launch-slot:' .. id)
    if not other then
        redis.call('SREM', KEYS[1], id)
    else
        local row = cjson.decode(other)
        if row.slot == slot then
            if row.state == 'exited' and seen[id] then used = used - 1 end
            if row.state ~= 'exited' and not seen[id] then
                if id == ARGV[7] and row.state == 'reserved' then previous = id
                else used = used + 1 end
            end
        end
    end
end
if used >= tonumber(ARGV[4]) then return {0, used} end
if previous then
    redis.call('DEL', ARGV[6] .. ':launch-slot:' .. previous)
    redis.call('SREM', KEYS[1], previous)
end
redis.call('SET', KEYS[2], cjson.encode({slot=slot, state='reserved'}), 'PX', ARGV[5])
redis.call('SADD', KEYS[1], execution)
return {1, used}
"""


@dataclass(frozen=True)
class SlotReading:
    accepted: bool
    sessions: int


@dataclass(frozen=True)
class LaunchSlots:
    redis: object

    def _call(self, mode: str, slot: str, execution: str, cap: int, ttl_ms: int, previous: str = "") -> SlotReading:
        from scripts.hive import registry
        from scripts.swarm.keyspace import ROOT

        accepted, used = self.redis.eval(
            _LAUNCH,
            3,
            f"{ROOT}:launch-slots",
            f"{ROOT}:launch-slot:{execution}",
            registry.INDEX,
            mode,
            slot,
            execution,
            cap,
            ttl_ms,
            ROOT,
            previous,
        )
        return SlotReading(bool(accepted), used)

    def reserve(self, slot: str, execution: str, cap: int, ttl_ms: int, previous: str = "") -> bool:
        return self.reserve_reading(slot, execution, cap, ttl_ms, previous).accepted

    def reserve_reading(self, slot: str, execution: str, cap: int, ttl_ms: int, previous: str = "") -> SlotReading:
        if type(cap) is not int or cap < 0 or type(ttl_ms) is not int or not 0 < ttl_ms <= 300000:
            raise ValueError("invalid launch slot cap or lifetime")
        return self._call("reserve", slot, execution, cap, ttl_ms, previous)

    def acknowledge(self, execution: str, slot: str = "") -> bool:
        """A named slot records a row that expired before its acknowledgement was read as live again."""
        return self._call("ack", slot, execution, 0, 45000).accepted

    def release(self, execution: str) -> bool:
        return self._call("release", "", execution, 0, 0).accepted

    def counts(self, seen: set[str] | None = None) -> dict[str, int]:
        import json

        from scripts.swarm.keyspace import ROOT

        counts: dict[str, int] = {}
        for execution in self.redis.smembers(f"{ROOT}:launch-slots"):
            raw = self.redis.get(f"{ROOT}:launch-slot:{execution}")
            if raw is None:
                self.redis.srem(f"{ROOT}:launch-slots", execution)
                continue
            row = json.loads(raw)
            reported = execution in (seen or set())
            delta = -int(reported) if row["state"] == "exited" else int(not reported)
            if delta:
                slot = row["slot"]
                counts[slot] = counts.get(slot, 0) + delta
        return counts

    def refresh(self, executions: list[str], ttl_ms: int = 45000) -> None:
        import json

        from scripts.swarm.keyspace import ROOT

        for execution in executions:
            key = f"{ROOT}:launch-slot:{execution}"
            raw = self.redis.get(key)
            if raw is not None and json.loads(raw)["state"] == "live":
                self.redis.pexpire(key, ttl_ms)

    def refresh_reported(self, now_ms: int) -> None:
        """Keep each live hive's reported rows of executions placed on that hive alive across two controller ticks,
        for members, whose Redis users cannot refresh them."""
        import json

        from scripts.hive import registry
        from scripts.swarm import lease
        from scripts.swarm.store import RedisStore

        store = RedisStore(self.redis)
        placed = {agent.execution_id: agent.hive for slug in store.slugs() for agent in store.agents(slug)}
        for hive in registry.hives(self.redis):
            if registry.live(hive, now_ms):
                report = json.loads(self.redis.get(f"{registry.key(hive['id'])}:sessions") or "{}")
                reported = [execution for ids in report.get("_executions", {}).values() for execution in ids]
                self.refresh(
                    [execution for execution in reported if placed.get(execution) == hive["id"]], 2 * lease.tick_ms()
                )
