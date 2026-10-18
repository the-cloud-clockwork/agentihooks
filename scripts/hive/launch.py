import json
import os
import time
from dataclasses import dataclass, replace

from scripts.hive import placement, placement_waits, registry
from scripts.hive.orders import Ack, LaunchOrder, Orders
from scripts.routing.reservation import LaunchSlots
from scripts.swarm import capacity, lease, profile_choice
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm.tick import Placed
from scripts.swarm_v2.runtime.base import Capability, Outcome, SpawnRequest, Status

HIVE = "hive"
ROLE_PROFILES = {"engineer": "eng", "cicd": "ci"}
TTL_MS = 300000
ORDERS = "controller-launch-orders"
ENDINGS = "launch-endings"


@dataclass(frozen=True)
class Selection:
    profile: profile_choice.ProfileDecision
    hive: str
    account: capacity.Account
    route_kind: str
    master_placement: placement.MasterPlacement | None = None


def loaded_hives(store: RedisStore) -> list[dict]:
    """Every registered hive with the worker agents it runs or is launching."""
    agents = [agent for slug in store.slugs() for agent in store.agents(slug)]
    hives = registry.hives(store.redis)
    for hive in hives:
        counts = json.loads(store.redis.get(f"{registry.key(hive['id'])}:sessions") or "{}")
        seen = {execution for executions in counts.get("_executions", {}).values() for execution in executions}
        reported = sum(
            count for key, count in counts.items() if key != "_executions" and not key.startswith("interactive:")
        )
        masters = sum(count for key, count in counts.items() if key.startswith("interactive:"))
        pending = sum(
            agent.hive == hive["id"]
            and agent.lane != "master"
            and agent.state in ("launching", "working", "suspect")
            and agent.execution_id not in seen
            for agent in agents
        )
        hive["agents"] = max(0, reported - masters) + pending
    return hives


class HiveLaunch:
    backend = HIVE
    capabilities = frozenset((Capability.SPAWN,))

    def __init__(self, store: RedisStore, slug: str, held: lease.Lease, herdr: object) -> None:
        self.store, self.slug, self.held, self.herdr = store, slug, held, herdr
        self.orders, self.slots = Orders(store.redis), LaunchSlots(store.redis)
        self.sleep = time.sleep

    def hives(self) -> list[dict]:
        return loaded_hives(self.store)

    def _record_wait(self, lane: str, task: dict, selected: Selection | placement.Wait) -> Selection | placement.Wait:
        reason = selected.reason if isinstance(selected, placement.Wait) else ""
        placement_waits.observe(self.store, self.slug, lane, task["id"], reason, lease.now_ms(self.store))
        return selected

    def select(self, config: SwarmConfig, lane: str, task: dict) -> Selection | placement.Wait:
        chosen = config.lanes.get(lane, {})
        saved = task.get("launch_assignment") or profile_choice.transfer(task)
        decision = self.herdr._profile_decision(config, lane, chosen, task, saved, saved, dict(os.environ))
        hives, now = self.hives(), lease.now_ms(self.store)
        hives = [hive for hive in hives if hive["id"] == task["hive"]] if task.get("hive") else hives
        role = ROLE_PROFILES.get(decision.profile, decision.profile)
        if (
            role in registry.UI_ROLES
            and lane != "master"
            and not any(hive["ui"] == "yes" and registry.live(hive, now) for hive in hives)
        ):
            return self._record_wait(lane, task, placement.Wait(placement.NO_UI_HIVE))
        harness = saved.get("harness") or chosen.get("agent") or "claude"
        if harness not in ("claude", "auto"):
            return self._record_wait(lane, task, placement.Wait("Codex hive placement is not enabled"))
        rows = getattr(self.herdr, "_quota_accounts", None)
        rows = capacity.accounts(dict(os.environ), now / 1000, refresh=False) if rows is None else rows
        if lane == "master" and not task.get("route_kind"):
            selected = self._master_selection(config, decision, hives, now, rows)
            return self._record_wait(lane, task, selected)
        account = saved.get("account") or self.herdr.planned_account(task["id"], "claude")
        candidates = [
            row
            for row in rows
            if row.harness == "claude" and capacity.free_seats(row) and (not account or row.name == account)
        ]
        route_kind = task.get("route_kind", "subscription")
        reason = placement.Wait("no advertised Claude account has room")
        for row in sorted(candidates, key=lambda row: (row.sessions, row.name)):
            hive = placement.place(role, "claude", row.name, route_kind, config.repo, hives, now)
            if isinstance(hive, str):
                return self._record_wait(lane, task, Selection(decision, hive, row, route_kind))
            reason = hive
        return self._record_wait(lane, task, reason)

    def _master_selection(self, config, decision, hives, now, rows):
        accounts = {row.name: row for row in rows if row.harness == "claude" and capacity.free_seats(row)}
        available = [
            {**hive, "slots": [slot for slot in hive["slots"] if slot["account"] in accounts]} for hive in hives
        ]
        selected = placement.place_master("claude", "", config.repo, available, now)
        if isinstance(selected, placement.Wait):
            return selected if hives else placement.Wait(placement.NO_UI_HIVE)
        account = (
            capacity.Account("claude", selected.account, "room", 0, None, None, 1, kind="interactive")
            if selected.route_kind == "interactive"
            else accounts[selected.account]
        )
        return Selection(decision, selected.hive, account, selected.route_kind, selected)

    def orders_for(self, task: str) -> LaunchOrder | None:
        raw = self.store.redis.hget(self.store.key(self.slug, ORDERS), task)
        return LaunchOrder(**json.loads(raw)) if raw else None

    def spawn(self, request: SpawnRequest) -> Outcome:
        lease.require_epoch(self.store, self.slug, request.task["controller_epoch"])
        if order := self.orders_for(request.task["id"]):
            previous = self.store.execution_registry.get(self.slug, order.execution_id)
            if previous.state in ("ended", "finished"):
                self.store.redis.hdel(self.store.key(self.slug, ORDERS), order.task)
            elif previous.name != request.name or previous.seat != request.task["seat"]:
                return Outcome("spawn", Status.REFUSED, HIVE, detail="another claimant owns the hive launch order")
            else:
                return self.wait(order)
        selected = self.select(request.config, request.lane, request.task)
        if isinstance(selected, placement.Wait):
            return Outcome("spawn", Status.UNAVAILABLE, HIVE, detail=selected.reason)
        admitted = self.admit(request, selected)
        if not self.slots.reserve(
            f"claude:{selected.account.name}", admitted.execution_id, selected.account.cap, TTL_MS
        ):
            self.end(admitted, "account reservation is full")
            return Outcome("spawn", Status.UNAVAILABLE, HIVE, detail="account reservation is full")
        prompt_ref = self.store.key(self.slug, f"launch-prompt:{admitted.execution_id}")
        task = {**request.task, "profile_decision": selected.profile.record()}
        order = LaunchOrder(
            self.slug,
            admitted.execution_id,
            admitted.generation,
            admitted.profile,
            request.lane,
            request.task["id"],
            selected.account.name,
            "claude",
            selected.route_kind,
            prompt_ref,
            self.held.epoch,
            lease.now_ms(self.store) + TTL_MS,
        )
        with self.store.redis.pipeline() as pipe:
            pipe.set(prompt_ref, json.dumps(task), px=TTL_MS)
            pipe.hset(self.store.key(self.slug, ORDERS), request.task["id"], json.dumps(order.__dict__))
            Orders(pipe).send(selected.hive, order)
            pipe.execute()
        return self.wait(order)

    def admit(self, request: SpawnRequest, selected: Selection) -> AgentRecord:
        agent = AgentRecord(
            request.name,
            request.lane,
            request.task["id"],
            seat=request.task["seat"],
            state="launching",
            profile=selected.profile.profile,
            profile_decision={
                **selected.profile.record(),
                **(
                    {"master_placement": {"hive": selected.hive, "headless": selected.master_placement.headless}}
                    if selected.master_placement is not None
                    else {}
                ),
            },
            harness="claude",
            account=selected.account.name,
            hive=selected.hive,
            runtime_target={"host_id": selected.hive},
            route_kind=selected.route_kind,
            desired_route_kind="interactive" if selected.master_placement is not None else "",
        )
        previous = self.store.execution_occupants(self.slug).get(agent.seat)
        ended = previous and (
            previous.state in ("ended", "finished")
            or self.store.redis.hexists(self.store.key(self.slug, ENDINGS), previous.execution_id)
        )
        with lease.fencing(self.held.epoch):
            return self.store.start_execution(self.slug, agent, previous.execution_id if ended else "")

    def wait(self, order: LaunchOrder) -> Outcome:
        while True:
            lease.require_epoch(self.store, self.slug, order.epoch)
            if ack := self.orders.ack(order):
                return self.complete(order, ack)
            if lease.now_ms(self.store) >= order.expires_at:
                return Outcome("spawn", Status.AMBIGUOUS, HIVE, detail="hive launch acknowledgement expired")
            self.sleep(1)

    def complete(self, order: LaunchOrder, ack: Ack) -> Outcome:
        agent = self.store.execution_registry.get(self.slug, order.execution_id)
        if ack.status != "live":
            if ack.failure != "ambiguous":
                self.end(agent, ack.failure)
                self.store.redis.hdel(self.store.key(self.slug, ORDERS), order.task)
            status = Status.AMBIGUOUS if ack.failure == "ambiguous" else Status.REFUSED
            return Outcome("spawn", status, HIVE, detail=f"hive launch failed: {ack.failure}")
        current = self.store.execution_occupants(self.slug).get(agent.seat)
        if (
            current is None
            or current.execution_id != order.execution_id
            or agent.generation != order.generation
            or agent.runtime_target != ack.target
            or agent.state != "working"
        ):
            raise SwarmError("hive launch acknowledgement does not match the live execution")
        if not self.slots.acknowledge(agent.execution_id):
            return Outcome("spawn", Status.AMBIGUOUS, HIVE, detail="hive launch reservation expired")
        placed = Placed(
            agent.pane_id,
            agent.harness,
            agent.account,
            model=agent.model,
            effort=agent.effort,
            profile=agent.profile,
            profile_decision=agent.profile_decision,
            execution=agent,
            route_kind=agent.route_kind,
            desired_route_kind=agent.desired_route_kind if agent.lane == "master" else None,
        )
        return Outcome("spawn", Status.OK, HIVE, placed)

    def end(self, agent: AgentRecord, reason: str) -> None:
        lease.require_epoch(self.store, self.slug, self.held.epoch)
        self.slots.release(agent.execution_id)
        self.store.redis.hset(
            self.store.key(self.slug, ENDINGS),
            agent.execution_id,
            json.dumps({"reason": reason, "generation": agent.generation}),
        )
        self.store.execution_registry.update(self.slug, replace(agent, state="ended"))
