import json
import sys
from collections.abc import Callable
from dataclasses import asdict, replace
from typing import Any

from scripts.events.bus import Bus
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.factory import open_bus
from scripts.hive import execution_events, execution_stop, presence, reports
from scripts.hub import lease
from scripts.hub.lifecycle import Lifecycle
from scripts.hub.registry import Reactor
from scripts.swarm.store import EVENTS_ENGINE, AgentRecord, RedisStore, connect

INTERVAL_MS = 300_000
BUCKET = "hub-reconciler"
CLAIMS = "spoke-work"
COMPLETED = "completed"
PENDING_STOPS = "pending-stops"


def _start(bus: Bus, slug: str, agent: AgentRecord) -> Envelope | None:
    key = execution_events.identity(slug, agent.name, agent.generation)
    entry = bus.for_swarm(slug).state(execution_events.STARTS).get(key)
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


def admitted_agents(store: Any, slug: str, legacy: list[AgentRecord]) -> list[AgentRecord]:
    """The agents of ``slug`` outside ``legacy``, the ``legacy_agents`` answer: those whose hive recorded their start
    on a real bus; none on the memory bus, where no start counts as recorded."""
    if BusConfig.from_environ().backend == "memory":
        return []
    kept = {agent.name for agent in legacy}
    return [agent for agent in store.agents(slug) if agent.name not in kept]


def stop_admitted(store: Any, slug: str, agent: AgentRecord, reason: str) -> str:
    """Sends the execution stop for ``agent`` to the hive that recorded its start, says on stderr what the stop
    answered and answers it; unknown on the memory bus, where no hive records a start, and failed when the bus
    refused the stop, which then waits in the swarm's pending stops for ``retry_stops``."""
    config = BusConfig.from_environ()
    if config.backend == "memory":
        return execution_stop.UNKNOWN
    try:
        bus = open_bus(config)
        try:
            outcome = execution_stop.stop(bus, slug, agent, reason)
        finally:
            bus.close()
    except Exception as exc:
        outcome = f"{execution_stop.FAILED} ({type(exc).__name__}: {exc})"
    print(f"hub execution stop for {agent.name} in swarm {slug} after {reason}: {outcome}", file=sys.stderr)
    pending = store.key(slug, PENDING_STOPS)
    if outcome.startswith(execution_stop.FAILED):
        store.redis.hset(pending, agent.name, json.dumps({"agent": asdict(agent), "reason": reason}))
    else:
        store.redis.hdel(pending, agent.name)
    return outcome


def retry_stops(store: Any, slug: str) -> list[str]:
    """Sends again every stop the bus refused in ``slug`` for an agent the swarm has since dropped, answering one
    action per stop it sent again; a refused stop for an agent still seated is discarded, since the next judgement of
    that agent decides afresh."""
    if BusConfig.from_environ().backend == "memory":
        return []
    pending = store.key(slug, PENDING_STOPS)
    seated = {agent.name for agent in store.agents(slug)}
    actions: list[str] = []
    for name, raw in store.redis.hgetall(pending).items():
        if name in seated:
            store.redis.hdel(pending, name)
            continue
        held = json.loads(raw)
        outcome = stop_admitted(store, slug, AgentRecord(**held["agent"]), held["reason"])
        actions.append(f"sent the {held['reason']} stop for {name} again: {outcome}")
    return actions


def event_slugs(store: RedisStore) -> list[str]:
    return [slug for slug in store.slugs() if store.redis.hget(store.key(slug, "config"), "engine") == EVENTS_ENGINE]


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
