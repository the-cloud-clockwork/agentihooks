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
CLAIMS = "spoke-work"
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


def event_slugs(store: RedisStore) -> list[str]:
    return [slug for slug in store.slugs() if store.config(slug).engine == "events"]


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


def _rebuilt(slug: str, agent: AgentRecord, hive: str, key: str, revision: int, task_generation: int) -> Envelope:
    return Envelope(
        id=f"started:{key}",
        type="execution.started",
        swarm=slug,
        entity="execution",
        key=agent.execution_id,
        version=agent.generation,
        time=max(agent.started_at, agent.launched_at),
        author=BUCKET,
        data={
            "task": agent.task,
            "task_generation": task_generation,
            "claim_key": key,
            "claim_revision": revision,
            "generation": agent.generation,
            "lane": agent.lane,
            "hive": hive,
            "name": agent.name,
            "pane_id": agent.pane_id,
            "harness": agent.harness,
            "account": agent.account,
        },
    )


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
        for slug in event_slugs(self._store):
            for agent in self._store.execution_registry.records(slug):
                if not guard():
                    return
                event = _start(self._bus, slug, agent) or self._restore(slug, agent, now)
                if event is None:
                    continue
                hive = agent.hive or agent.runtime_target.get("host_id")
                ending = (
                    _ending(self._bus, snapshots, hive, event, agent, now) if hive == event.data.get("hive") else None
                )
                if ending is not None:
                    self._lifecycle.handle(ending)
        if not guard():
            return
        value = str(now).encode()
        if entry is None:
            self._state.create(COMPLETED, value)
        else:
            self._state.update(COMPLETED, value, entry.revision)

    def _restore(self, slug: str, agent: AgentRecord, now: int) -> Envelope | None:
        # Only spokes beat presence, so a hive without one launched by push and its executions stay with the tick.
        hive = agent.hive or agent.runtime_target.get("host_id")
        key = execution_events.identity(slug, agent.name, agent.generation)
        if not hive or self._bus.state(presence.BUCKET).get(hive) is None or self._launching(key, agent, now):
            return None
        row = self._lifecycle.ledger.task(slug, agent.task)
        if not row or row.get("state") not in {"claimed", "pr"} or row.get("claimed_by") != agent.name:
            return None
        claims = self._bus.state(CLAIMS)
        entry = claims.get(key)
        value = json.loads(entry.value) if entry else {}
        if "event" in value:
            event = Envelope.from_dict(value["event"])
        else:
            claimed = json.dumps({"owner": hive, "state": "claimed"}).encode()
            revision = entry.revision if entry else claims.create(key, claimed)
            event = _rebuilt(slug, agent, hive, key, revision, row.get("claim_generation", 0))
            value = {"state": "started", "claim_revision": revision, "event": event.to_dict()}
            claims.update(key, json.dumps(value).encode(), revision)
        execution_events.started(self._bus, event)
        return event

    def _launching(self, key: str, agent: AgentRecord, now: int) -> bool:
        entry = self._bus.state(execution_events.STARTS).get(key)
        if entry is None:
            return False
        return json.loads(entry.value) is not None or now - max(agent.started_at, agent.launched_at) < INTERVAL_MS
