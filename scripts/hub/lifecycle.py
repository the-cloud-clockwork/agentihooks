import json
from collections.abc import Callable
from copy import copy

from scripts.events.bus import Bus
from scripts.events.envelope import Envelope
from scripts.hive.launch import release as release_slot
from scripts.hub.registry import Reactor
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import AgentRecord, RedisStore, SwarmError, connect

_RELEASE_CLAIM = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""


class Lifecycle:
    def __init__(
        self,
        bus: Bus,
        store: RedisStore | None = None,
        ledger: LedgerClient | None = None,
        release: Callable[[RedisStore, str, AgentRecord], None] = release_slot,
    ):
        self.bus = bus
        self.store = store
        self.ledger = ledger or LedgerClient(service=True)
        self.release = release

    def reactor(self) -> Reactor:
        return Reactor("lifecycle", "swarm.*.execution.exited", self.handle, singleton=True)

    def handle(self, event: Envelope) -> None:
        if event.type != "execution.exited" or event.entity != "execution":
            return
        claims = self.bus.for_swarm(event.swarm).state("spoke-work")
        key = event.data.get("claim_key")
        if not key:
            return
        entry = claims.get(key)
        if entry is None:
            return
        value = json.loads(entry.value)
        if value.get("claim_revision", entry.revision) != event.data["claim_revision"]:
            return
        if value["state"] not in {"launching", "started", "exiting"}:
            return
        self.store = self.store or connect()
        try:
            agent = self.store.execution_registry.get(event.swarm, event.key)
        except SwarmError:
            return
        if (agent.execution_id, agent.generation, agent.task, agent.name) != (
            event.key,
            event.version,
            event.data["task"],
            event.data["name"],
        ):
            return
        revision = entry.revision
        if value["state"] != "exiting":
            value = {
                **value,
                "state": "exiting",
                "claim_revision": event.data["claim_revision"],
                "execution": event.key,
                "generation": event.version,
                "returned": False,
            }
            revision = claims.update(key, json.dumps(value).encode(), revision)
        if (value["execution"], value["generation"]) != (event.key, event.version):
            return
        if not value["returned"]:
            self._refund(event.swarm, agent)
            if not self.store.drop_agent(
                event.swarm, agent.name, expected=agent, before=lambda pipe: self._return(pipe, event, agent)
            ):
                return
            value = {**value, "returned": True}
            revision = claims.update(key, json.dumps(value).encode(), revision)
        self._settle(event, agent)
        claims.update(key, json.dumps({**value, "state": "exited"}).encode(), revision)

    def _refund(self, slug: str, agent: AgentRecord) -> None:
        from scripts.swarm import tick

        row = self.ledger.task(slug, agent.task) or {}
        rows = {dep: self.ledger.task(slug, dep) or {} for dep in row.get("parked_on") or []}
        tick._refund_parked(slug, self.store, {**rows, agent.task: row}, agent)

    def _return(self, pipe, event: Envelope, agent: AgentRecord) -> None:
        store = copy(self.store)
        store.redis = pipe
        self.release(store, event.swarm, agent)
        if not self.store.authority_held(event.swarm, agent.task):
            pipe.eval(_RELEASE_CLAIM, 1, store.key(event.swarm, "claim", agent.task), agent.name)

    def _settle(self, event: Envelope, agent: AgentRecord) -> None:
        row = self.ledger.task(event.swarm, agent.task)
        if not row or row["state"] not in {"claimed", "pr"} or row.get("claimed_by") != agent.name:
            return
        if row.get("awaiting") == "merge" or row.get("claim_generation", 0) != event.data["task_generation"]:
            return
        fields = {"state": "done"} if row.get("done") else {"state": "open", "claimed_by": ""}
        self.ledger.update_task(
            event.swarm,
            agent.task,
            fields,
            if_state=("claimed", "pr"),
            if_match={"claimed_by": agent.name, "claim_generation": event.data["task_generation"]},
        )
