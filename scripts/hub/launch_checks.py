"""Launch checks for the workers the hub admits on the events engine: each is timed from the launch its hive reports,
a spawn that reports no launch is failed one launch window after admission, and a failed worker is stopped on its
hive."""

import sys
import traceback
from collections.abc import Callable

from scripts.events.config import BusConfig
from scripts.hub import lease
from scripts.hub.registry import Reactor
from scripts.swarm import launch_check
from scripts.swarm.runtime import SPAWN_TIMEOUT_S
from scripts.swarm.store import AgentRecord, RedisStore, connect

INTERVAL_MS = 10_000
LAUNCH_WINDOW_MS = SPAWN_TIMEOUT_S * 1000

Stop = Callable[[str, AgentRecord, str], None]


def hung(entry: dict, now: int) -> dict:
    """The miss of a spawn whose hive reported no launch one launch window after its check began, else none."""
    if now - entry["at"] < LAUNCH_WINDOW_MS:
        return {}
    window = f"a launch reported within {LAUNCH_WINDOW_MS // 1000} seconds of admission"
    return {"launched": {"expected": window, "actual": "none reported"}}


class LaunchChecks:
    def __init__(self, stop: Stop, store: RedisStore | None = None, clock: lease.Clock = lease.wall_ms) -> None:
        self._stop, self._store, self._clock = stop, store, clock
        self._due = 0

    def reactor(self) -> Reactor:
        return Reactor(
            "launch-checks", "swarm.*.launch-checks.requested", lambda _: None, singleton=True, poll=self.poll
        )

    def poll(self, guard: Callable[[], bool] = lambda: True) -> None:
        now = self._clock()
        if now < self._due or not guard():
            return
        if self._store is None and BusConfig.from_environ().backend == "memory":
            return
        self._store = self._store or connect()
        for slug in self._store.slugs():
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
        store = self._store or connect()
        waiting = {
            name: entry
            for name, entry in launch_check.pending(store, slug).items()
            if entry.get("owner") == launch_check.HUB
        }
        if not waiting:
            return []
        agents = {agent.name: agent for agent in store.agents(slug)}
        actions: list[str] = []
        for name, entry in waiting.items():
            agent = agents.get(name)
            if agent is None:
                launch_check.forget(store, slug, name)
                continue
            if agent.launched_at:
                found, elapsed = launch_check.hive_misses(store, agent), now - agent.launched_at
                if found and elapsed < launch_check.DEADLINE_MS:
                    continue
            else:
                found, elapsed = hung(entry, now), now - entry["at"]
                if not found:
                    continue
            actions.append(self._judge(slug, store, agent, entry, found, now, elapsed))
        return actions

    def _judge(
        self, slug: str, store: RedisStore, agent: AgentRecord, entry: dict, found: dict, now: int, elapsed: int
    ) -> str:
        launch_check.record(store, slug, agent, found, now, elapsed)
        launch_check.forget(store, slug, agent.name)
        if set(found) <= launch_check.REPORT_ONLY:
            launch_check.clear_relaunched(store, slug, agent.task)
            if not found:
                return f"{agent.name} passed its launch check in {elapsed // 1000} seconds"
            return f"{agent.name} failed its launch check on {', '.join(found)}; reported only"
        fields = ", ".join(found)
        self._stop(slug, agent, f"its launch check failed on {fields}")
        if entry["relaunch"] and not launch_check.relaunched(store, slug, agent.task):
            launch_check.mark_relaunched(store, slug, agent.task)
            return f"stopped {agent.name} after its launch check failed on {fields}; its task is relaunched once"
        return f"stopped {agent.name} after its launch check failed on {fields}; its one relaunch is spent"
