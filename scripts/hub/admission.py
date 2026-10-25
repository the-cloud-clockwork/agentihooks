"""Lane work admission the hub answers for hive spokes: a member's Redis user cannot claim a task, mint an agent name
or occupy a seat, and a remote hive makes no service ledger reads."""

import json
from collections.abc import Callable
from types import SimpleNamespace

from scripts.hub import lease
from scripts.swarm import readiness
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import AgentRecord, RedisStore, connect

ADMITTED = "spoke-admission"
# Long enough for every redelivery of one offer; a reopen raises the claim generation, so a new offer has a new key.
ADMITTED_MS = 24 * 60 * 60 * 1000


class Admission:
    def __init__(
        self,
        store: RedisStore | None = None,
        ledger: LedgerClient | None = None,
        source: readiness.Source | None = None,
        clock: lease.Clock = lease.wall_ms,
    ):
        self._store, self.ledger, self.clock = store, ledger or LedgerClient(service=True), clock
        self.source = source or readiness.Source(store, self.ledger)

    @property
    def store(self) -> RedisStore:
        self._store = self._store or connect()
        return self._store

    def _key(self, slug: str, request: dict) -> str:
        return self.store.key(slug, ADMITTED, request["task"], str(request["generation"]))

    def admit(self, slug: str, request: dict) -> dict | None:
        held = self.store.redis.get(self._key(slug, request))
        if held:
            admitted = json.loads(held)
            return admitted if admitted["hive"] == request["hive"] else None
        task_id, lane, generation = request["task"], request["lane"], request["generation"]
        view = self.source.snapshot(slug)
        if readiness.withdrawal(SimpleNamespace(key=task_id, version=generation, data={"lane": lane}), view):
            return None
        return self._claim(slug, request, view.rows[task_id])

    def _claim(self, slug: str, request: dict, row: dict) -> dict | None:
        from scripts.swarm import tick

        task_id, lane, now = request["task"], request["lane"], self.clock()
        name = self.store.next_name(slug, lane, now)
        if not self.store.claim(slug, task_id, name, tick.LEASE_MS):
            return None
        taken = {agent.seat for agent in self.store.agents(slug)}
        seat = tick._free_seat(slug, lane, taken, self.store.handoff_seat(slug, task_id))
        record = AgentRecord(name, lane, task_id, seat=seat, state="starting", started_at=now, hive=request["hive"])
        agent = self.store.start_execution(slug, record)
        fields = {"state": "pr" if row.get("pr_url") else "claimed", "claimed_by": name}
        expected = {"claimed_by": row.get("claimed_by", ""), "claim_generation": request["generation"]}
        live = self.ledger.update_task(slug, task_id, fields, if_state=("open",), if_match=expected)
        if live.get("claimed_by") != name:
            self.store.release(slug, task_id, name)
            self.store.drop_agent(slug, name)
            return None
        self.store.seats.occupy(seat, name, now)
        task = tick.primed(self.store, slug, seat, {**row, **live})
        identity = {"execution_id": agent.execution_id, "execution_generation": agent.generation}
        admitted = {
            "hive": request["hive"],
            "name": name,
            "task": {**task, **identity, "claim_generation": request["generation"]},
        }
        self.store.redis.set(self._key(slug, request), json.dumps(admitted), px=ADMITTED_MS)
        return admitted

    def reopen(self, slug: str, request: dict) -> bool:
        key = self._key(slug, request)
        held = self.store.redis.get(key)
        if not held or json.loads(held)["hive"] != request["hive"]:
            return False
        name = json.loads(held)["name"]
        self.store.release(slug, request["task"], name)
        self.store.drop_agent(slug, name)
        self.ledger.update_task(
            slug,
            request["task"],
            {"state": "open", "claimed_by": ""},
            if_state=("claimed", "pr"),
            if_match={"claimed_by": name},
        )
        self.store.redis.delete(key)
        return True


OPERATIONS: dict[str, Callable[[Admission, str, dict], object]] = {"admit": Admission.admit, "reopen": Admission.reopen}
