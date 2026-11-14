import json
from collections.abc import Callable
from copy import copy
from dataclasses import asdict
from typing import Any, cast

from scripts.events.bus import Bus
from scripts.events.envelope import Envelope
from scripts.hive.launch import release as release_slot
from scripts.hub.registry import Reactor
from scripts.swarm import dead_resume
from scripts.swarm.dead_resume import FRESH_NOTICE, RESUME_TEXT
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
        ) or (
            value["state"] == "started"
            and self._resumed(
                event, agent, cast(Any, claims), cast(str, key), cast(dict[str, Any], value), entry.revision
            )
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

    def _resumed(
        self, event: Envelope, agent: AgentRecord, claims: Any, key: str, value: dict[str, Any], revision: int
    ) -> bool:
        """Order the agent's hive to reopen its own conversation in its seat, at most RECOVERY_ATTEMPTS times per
        execution and only while it holds its claim, claim generation and seat; True while that resume is ordered and
        unanswered or the exit predates it, False when the exit goes on to drop the agent and reopen its task."""
        from scripts.hive.orders import DONE, CommandOrder, Orders
        from scripts.swarm.resume import RECOVERY_ATTEMPTS

        target: dict[str, Any] = cast(Any, agent).runtime_target
        data: dict[str, Any] = cast(Any, event).data
        hive: str = agent.hive or target.get("host_id") or ""
        if not hive or data.get("reason") == "lost":
            return False
        orders, pending = Orders(cast(Any, self.store).redis), value.get("resume")
        if pending and pending["launched_at"] == agent.launched_at:
            ack = orders.command(hive, CommandOrder(**pending["order"]))
            if ack is not None and ack.status != DONE:
                ledger: Any = self.ledger
                ledger.notify(event.swarm, FRESH_NOTICE.format(name=agent.name, reason=f"resume failed: {ack.failure}"))
            return ack is None or ack.status == DONE
        if pending and event.time < agent.launched_at:
            return True
        attempts: int = value.get("resumes", 0)
        if attempts >= RECOVERY_ATTEMPTS or not self._owned(event, agent):
            return False
        order = CommandOrder(
            f"resume:{agent.execution_id}:{agent.generation}:{attempts + 1}",
            "resume",
            event.swarm,
            agent.name,
            agent.execution_id,
            agent.generation,
            RESUME_TEXT,
        )
        resume = {"order": asdict(order), "launched_at": agent.launched_at}
        claims.update(key, json.dumps({**value, "resumes": attempts + 1, "resume": resume}).encode(), revision)
        orders.command(hive, order)
        return True

    def _owned(self, event: Envelope, agent: AgentRecord) -> bool:
        """Whether the dead agent may reopen: the swarm runs, its task row still names it at the exit's claim
        generation, it holds its seat and claim, and no handoff or compact limit sends the task to a fresh agent."""
        store = cast(RedisStore, self.store)
        fences: Any = dead_resume
        ledger: Any = self.ledger
        data: dict[str, Any] = cast(Any, event).data
        row: dict[str, Any] = ledger.task(event.swarm, agent.task) or {}
        return bool(
            store.config(event.swarm).state not in {"stopped", "stopping"}
            and row.get("claim_generation", 0) == data["task_generation"]
            and not fences._reason(store, event.swarm, agent)
            and fences._owns(store, event.swarm, agent, ledger)
        )

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
