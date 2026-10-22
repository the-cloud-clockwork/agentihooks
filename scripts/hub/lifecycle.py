import json
from collections.abc import Callable

from scripts.events.bus import Bus
from scripts.events.envelope import Envelope
from scripts.hive.launch import release as release_slot
from scripts.hub.registry import Reactor
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import AgentRecord, RedisStore, connect


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
        claims = self.bus.state("spoke-work")
        key = event.data["claim_key"]
        entry = claims.get(key)
        if entry is None:
            return
        value = json.loads(entry.value)
        if value.get("claim_revision", entry.revision) != event.data["claim_revision"]:
            return
        if value["state"] not in {"launching", "started", "exiting"}:
            return
        self.store = self.store or connect()
        agent = self.store.execution_registry.get(event.swarm, event.key)
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
                "execution": event.key,
                "generation": event.version,
                "returned": False,
            }
            revision = claims.update(key, json.dumps(value).encode(), revision)
        if (value["execution"], value["generation"]) != (event.key, event.version):
            return
        if not value["returned"]:
            self.release(self.store, event.swarm, agent)
            self.store.release(event.swarm, agent.task, agent.name)
            self.store.drop_agent(event.swarm, agent.name)
            value = {**value, "returned": True}
            revision = claims.update(key, json.dumps(value).encode(), revision)
        self._settle(event, agent)
        claims.update(key, json.dumps({**value, "state": "exited"}).encode(), revision)

    def _settle(self, event: Envelope, agent: AgentRecord) -> None:
        row = self.ledger.task(event.swarm, agent.task)
        if not row or row["state"] not in {"claimed", "pr"} or row.get("claimed_by") != agent.name:
            return
        if row.get("awaiting") == "merge":
            return
        fields = {"state": "done"} if row.get("done") else {"state": "open", "claimed_by": ""}
        self.ledger.update_task(
            event.swarm,
            agent.task,
            fields,
            if_state=("claimed", "pr"),
            if_match={"claimed_by": agent.name, "claim_generation": row.get("claim_generation", 0)},
        )
