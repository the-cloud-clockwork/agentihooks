import json
from collections.abc import Callable
from dataclasses import replace

from scripts.events.bus import Bus
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.factory import open_bus
from scripts.hive import execution_events, presence, reports
from scripts.hub import lease
from scripts.hub.lifecycle import Lifecycle
from scripts.hub.registry import Reactor
from scripts.swarm.store import AgentRecord, RedisStore, connect

INTERVAL_MS = 300_000
BUCKET = "hub-reconciler"
COMPLETED = "completed"


def _start(bus: Bus, slug: str, agent: AgentRecord) -> Envelope | None:
    key = execution_events.identity(slug, agent.name, agent.generation)
    entry = bus.state(execution_events.STARTS).get(key)
    raw = json.loads(entry.value) if entry else None
    if raw is None:
        return None
    event = Envelope.from_dict(raw)
    if not event.data.get("claim_key") or event.type != "execution.started":
        return None
    if (event.swarm, event.key, event.version, event.data.get("name"), event.data.get("task")) != (
        slug,
        agent.execution_id,
        agent.generation,
        agent.name,
        agent.task,
    ):
        return None
    return event


def legacy_agents(store: RedisStore, slug: str) -> list[AgentRecord]:
    agents = store.agents(slug)
    config = BusConfig.from_environ()
    if config.backend == "memory" or not agents:
        return agents
    bus = open_bus(config)
    try:
        return [agent for agent in agents if _start(bus, slug, agent) is None]
    finally:
        bus.close()


def _exited(report: dict | None, event: Envelope, agent: AgentRecord, now: int) -> bool:
    if report is None:
        return False
    observed = report["observed_at"]
    if not max(agent.started_at, agent.launched_at, event.time, now - INTERVAL_MS) <= observed <= now:
        return False
    record = report["executions"].get(agent.execution_id)
    if record is not None:
        return (record["slug"], record["name"], record["generation"]) == (
            event.swarm,
            agent.name,
            agent.generation,
        ) and record["pane"]["state"] in execution_events.ENDED
    panes = report.get("pane_ids")
    return isinstance(panes, list) and bool(agent.pane_id) and agent.pane_id not in panes


def _ending(
    bus: Bus, snapshots: dict[str, dict], hive: str, event: Envelope, agent: AgentRecord, now: int
) -> Envelope | None:
    if presence.lost(bus, hive, now):
        data = {**event.data, "reason": "lost"}
        return replace(event, id=f"lost:{event.id}", type="execution.exited", time=now, cause=event.id, data=data)
    if not _exited(snapshots.get(hive), event, agent, now):
        return None
    time = snapshots[hive]["observed_at"]
    return replace(event, id=f"exited:{event.id}", type="execution.exited", time=time, cause=event.id)


class Reconciler:
    def __init__(
        self,
        bus: Bus,
        store: RedisStore | None = None,
        lifecycle: Lifecycle | None = None,
        clock: lease.Clock = lease.wall_ms,
    ):
        self._bus = bus
        self._state = bus.state(BUCKET)
        self._store = store
        self._lifecycle = lifecycle
        self._clock = clock

    def reactor(self) -> Reactor:
        return Reactor("reconciler", "swarm.*.reconcile.requested", lambda _: None, singleton=True, poll=self.poll)

    def poll(self, guard: Callable[[], bool] = lambda: True) -> None:
        if not guard():
            return
        entry = self._state.get(COMPLETED)
        now = self._clock()
        if entry is not None and now < int(entry.value) + INTERVAL_MS:
            return
        if self._store is None:
            if BusConfig.from_environ().backend == "memory":
                return
            self._store = connect()
        self._lifecycle = self._lifecycle or Lifecycle(self._bus, self._store)
        snapshots = reports.Reports(self._store.redis).live()
        for slug in self._store.slugs():
            for agent in self._store.execution_registry.records(slug):
                if not guard():
                    return
                event = _start(self._bus, slug, agent)
                if event is None:
                    continue
                hive = agent.hive or agent.runtime_target.get("host_id")
                ending = _ending(self._bus, snapshots, hive, event, agent, now) if hive == event.data.get("hive") else None
                if ending is not None:
                    self._lifecycle.handle(ending)
        if not guard():
            return
        value = str(now).encode()
        if entry is None:
            self._state.create(COMPLETED, value)
        else:
            self._state.update(COMPLETED, value, entry.revision)
