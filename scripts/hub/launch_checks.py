"""Launch checks for the workers the hub admits on the events engine, timed on the hub clock alone: the join deadline
runs from the poll that first reads the execution's started record, and a spawn the hive never started fails one spawn
timeout and one deadline after the hive began it. A failed worker is stopped on its hive and its check is kept until
its exit drops it; a worker no hive started is freed and its task reopened."""

import json
import sys
import traceback
from collections.abc import Callable
from importlib import import_module
from typing import Any, cast

from scripts.events.bus import Bus
from scripts.events.config import BusConfig
from scripts.hive import execution_events, execution_stop
from scripts.hub import lease
from scripts.hub.registry import Reactor
from scripts.swarm.runtime import SPAWN_TIMEOUT_S
from scripts.swarm.store import AgentRecord, RedisStore

launch_check: Any = import_module("scripts.swarm.launch_check")
store_module: Any = import_module("scripts.swarm.store")

INTERVAL_MS = 10_000
HUNG_MS = SPAWN_TIMEOUT_S * 1000 + launch_check.DEADLINE_MS
STARTED, PREPARING, STOPPING, REASON = "started", "preparing", "stopping", "reason"

Stop = Callable[[Bus, str, AgentRecord, str], str]
Misses = dict[str, dict[str, Any]]


def hung(since: int, now: int) -> Misses:
    """The miss of a spawn that has not started one hung window after ``since``, else none."""
    if now - since < HUNG_MS:
        return {}
    window = f"a started execution within {HUNG_MS // 1000} seconds of the hive taking it"
    return {"launched": {"expected": window, "actual": "none started"}}


class LaunchChecks:
    def __init__(
        self,
        bus: Bus,
        store: RedisStore | None = None,
        admission: Any = None,
        clock: lease.Clock = lease.wall_ms,
        stop: Stop = execution_stop.stop,
    ) -> None:
        self._bus, self._store, self._admission, self._clock, self._stop = bus, store, admission, clock, stop
        self._due = 0

    def reactor(self) -> Reactor:
        return Reactor(
            "launch-checks", "swarm.*.launch-checks.requested", lambda _: None, singleton=True, poll=self.poll
        )

    def _connected(self) -> RedisStore:
        if self._store is None:
            self._store = cast(RedisStore, store_module.connect())
        return self._store

    def _freer(self) -> Any:
        if self._admission is None:
            from scripts.hub.admission import Admission

            self._admission = Admission(self._connected())
        return self._admission

    def poll(self, guard: Callable[[], bool] = lambda: True) -> None:
        now = self._clock()
        if now < self._due or not guard():
            return
        if self._store is None and BusConfig.from_environ().backend == "memory":
            return
        store = self._connected()
        for slug in cast(list[str], store.slugs()):
            if not guard():
                return
            try:
                for action in self.check(slug, now):
                    print(f"hub: launch checks swarm={slug}: {action}", file=sys.stderr)
            except Exception:
                print(f"hub: launch checks swarm={slug} failed", file=sys.stderr)
                traceback.print_exc()
        self._due = now + INTERVAL_MS

    def check(self, slug: str, now: int) -> list[str]:
        """Judges every hub owned launch check in the swarm and returns what it did."""
        store = self._connected()
        pending = cast(dict[str, dict[str, Any]], launch_check.pending(store, slug))
        waiting = {name: entry for name, entry in pending.items() if entry.get("owner") == launch_check.HUB}
        if not waiting:
            return []
        listed: list[AgentRecord] = cast(Any, store).agents(slug)
        agents = {agent.name: agent for agent in listed}
        actions: list[str] = []
        for name, entry in waiting.items():
            agent = agents.get(name)
            if agent is None:
                launch_check.forget(store, slug, name)
            elif STOPPING in entry:
                if now - int(entry[STOPPING]) >= launch_check.DEADLINE_MS:
                    actions.append(self._halt(slug, agent, entry, now))
            elif (verdict := self._verdict(slug, agent, entry, now)) is not None:
                actions.append(self._judge(slug, agent, entry, verdict, now))
        return actions

    def _save(self, slug: str, name: str, entry: dict[str, Any]) -> dict[str, Any]:
        store: Any = self._connected()
        store.redis.hset(store.key(slug, "launch-checks"), name, json.dumps(entry))
        return entry

    def _verdict(self, slug: str, agent: AgentRecord, entry: dict[str, Any], now: int) -> tuple[Misses, int] | None:
        """The misses and elapsed hub time of a check that is due, or None while it still waits."""
        start = execution_stop.started(self._bus, slug, agent.name, agent.generation)
        if start is not None:
            if STARTED not in entry:
                entry = self._save(slug, agent.name, {**entry, STARTED: now})
            found = cast(Misses, launch_check.hive_misses(self._connected(), agent))
            elapsed = now - int(entry[STARTED])
            return None if found and elapsed < launch_check.DEADLINE_MS else (found, elapsed)
        identity = execution_events.identity(slug, agent.name, agent.generation)
        began = self._bus.for_swarm(slug).state(execution_events.STARTS).get(identity) is not None
        if PREPARING not in entry and began:
            entry = self._save(slug, agent.name, {**entry, PREPARING: now})
        since = int(entry.get(PREPARING, entry["at"]))
        found = hung(since, now)
        return (found, now - since) if found else None

    def _judge(
        self, slug: str, agent: AgentRecord, entry: dict[str, Any], verdict: tuple[Misses, int], now: int
    ) -> str:
        store = self._connected()
        found, elapsed = verdict
        launch_check.record(store, slug, agent, found, now, elapsed)
        if set(found) <= launch_check.REPORT_ONLY:
            launch_check.forget(store, slug, agent.name)
            if not found:
                return f"{agent.name} passed its launch check in {elapsed // 1000} seconds"
            return f"{agent.name} failed its launch check on {', '.join(found)}; reported only"
        return self._halt(slug, agent, {**entry, REASON: f"its launch check failed on {', '.join(found)}"}, now)

    def _halt(self, slug: str, agent: AgentRecord, entry: dict[str, Any], now: int) -> str:
        """Stops the agent on its hive, keeping the check until its exit drops the agent; frees an agent no hive
        started. A raising stop leaves the check stopping, so the next deadline sends it again."""
        self._save(slug, agent.name, {**entry, STOPPING: now})
        outcome = self._stop(self._bus, slug, agent, str(entry[REASON]))
        if outcome != execution_stop.UNKNOWN:
            return f"stop {outcome} for {agent.name} because {entry[REASON]}; held until its exit"
        self._freer().free(slug, agent)
        launch_check.forget(self._connected(), slug, agent.name)
        return f"freed {agent.name} and reopened {agent.task} because {entry[REASON]} and no hive started it"
