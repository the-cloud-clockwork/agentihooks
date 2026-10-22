import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from scripts.hive import admission, headroom, orders, placement, placement_waits, registry
from scripts.hive.orders import Ack, CommandOrder, LaunchOrder, Orders
from scripts.routing.reservation import LaunchSlots
from scripts.swarm import capacity, lease, profile_choice
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm.tick import ACK_LATER, Placed
from scripts.swarm_v2 import front_door
from scripts.swarm_v2.runtime.base import Capability, Outcome, SpawnRequest, Status

HIVE = "hive"
ROLE_PROFILES = {"engineer": "eng", "cicd": "ci", "planner": "plan", "dispatcher": "dispatch"}
TTL_MS = 300000
ORDERS = "controller-launch-orders"
ENDINGS = "launch-endings"
PENDING = "hive launch ordered, waiting for its acknowledgement"
GONE = "the hive launch order ended before its acknowledgement"
EXPIRED = "hive launch acknowledgement expired"
LATE = "hive launch reservation expired"


@dataclass(frozen=True)
class Selection:
    profile: profile_choice.ProfileDecision
    hive: str
    account: capacity.Account
    route_kind: str
    master_placement: placement.MasterPlacement | None = None
    fallback: str = ""
    origin: str = ""


def loaded_hives(store: RedisStore) -> list[dict]:
    """Every registered hive with the worker agents it runs or is launching.

    A hive counts a running agent in its slot's processes even when it lists no execution for it."""
    agents = [agent for slug in store.slugs() for agent in store.agents(slug)]
    hives, now = registry.hives(store.redis), lease.now_ms(store)
    for hive in hives:
        counts = json.loads(store.redis.get(f"{registry.key(hive['id'])}:sessions") or "{}")
        executions = counts.get("_executions", {})
        seen = {execution for ids in executions.values() for execution in ids}
        reported = {
            key: count for key, count in counts.items() if key != "_executions" and not key.startswith("interactive:")
        }
        masters = sum(count for key, count in counts.items() if key.startswith("interactive:"))
        pending: dict[str, int] = {}
        for agent in agents:
            if (
                agent.hive == hive["id"]
                and agent.lane != "master"
                and agent.state in ("launching", "working", "suspect")
                and agent.execution_id not in seen
            ):
                slot = f"{agent.harness}:{agent.account}"
                pending[slot] = pending.get(slot, 0) + 1
        unclaimed = {
            slot: max(0, reported.get(slot, 0) - counts.get(f"interactive:{slot}", 0) - len(executions.get(slot, [])))
            for slot in pending
        }
        hive["agents"] = max(0, sum(reported.values()) - masters) + sum(
            max(0, count - unclaimed[slot]) for slot, count in pending.items()
        )
        found = admission.capped(
            admission.room(
                headroom.read(store.redis, hive["id"], now), pending=admission.pending(store.redis, hive["id"])
            ),
            hive["agents"],
            hive["max_agents"],
        )
        hive["room"] = found.room
        hive["refusal"] = admission.refusal(found)
    return hives


def _key(account: str) -> str:
    """A hive names an account by its AH_CC_TOKEN_ slug, a hosted controller by its Secret key; only _ and - differ."""
    return account.replace("_", "-")


def _slot_cap(hives: list[dict], selected: placement.MasterPlacement, fallback: int | None) -> int | None:
    """A master on a hive is held to the session cap that hive reports for the account, not the cloud worker cap."""
    caps = [
        slot["cap"]
        for hive in hives
        if hive["id"] == selected.hive
        for slot in hive["slots"]
        if slot["account"] == selected.account and "cap" in slot
    ]
    return caps[0] if caps else fallback


def _by_origin(hives: list[dict], repo: str) -> list[dict]:
    """A hive that lists the origin of the swarm's checkout lists the swarm's repo, whatever path it keeps it at."""
    from scripts.hive.daemon import origin

    identity = origin(Path(repo))
    return [
        {**hive, "repos": [*hive["repos"], repo], orders.ORIGIN: identity}
        if identity and identity in hive["repos"] and repo not in hive["repos"]
        else hive
        for hive in hives
    ]


def _matched_origin(hives: list[dict], hive: str, repo: str) -> str | placement.Wait:
    from scripts.hive.daemon import origin

    found = next((found for found in hives if found["id"] == hive), None)
    if found is None:
        return placement.Wait(f"hive {hive} is not in the hive listing for {origin(Path(repo)) or repo}")
    return found.get(orders.ORIGIN, "")


def _first_choice(
    candidates: list[capacity.Account], choose: Callable[[str], placement.Choice | placement.Wait]
) -> tuple[capacity.Account, placement.Choice] | placement.Wait:
    """An account a hive takes outright wins over an earlier account that only a fallback takes."""
    reason, fallen = placement.Wait("no advertised Claude account has room"), None
    for row in sorted(candidates, key=lambda row: (row.sessions, row.name)):
        choice = choose(row.name)
        if isinstance(choice, placement.Wait):
            reason = choice
        elif not choice.fallback:
            return row, choice
        else:
            fallen = fallen or (row, choice)
    return fallen or reason


def _match(check: str, expected: object, actual: object) -> None:
    if expected != actual:
        raise SwarmError(
            f"hive launch acknowledgement does not match the live execution: "
            f"{check} expected {expected!r}, got {actual!r}"
        )


def release(store: RedisStore, slug: str, agent: AgentRecord) -> None:
    LaunchSlots(store.redis).release(agent.execution_id)
    admission.release(store.redis, agent.hive, f"{slug}/{agent.name}")


class HiveLaunch:
    backend = HIVE
    capabilities = frozenset((Capability.SPAWN,))

    def __init__(self, store: RedisStore, slug: str, held: lease.Lease, herdr: object) -> None:
        self.store, self.slug, self.held, self.herdr = store, slug, held, herdr
        self.orders, self.slots = Orders(store.redis), LaunchSlots(store.redis)
        self.sleep = time.sleep
        self.opening = orders.build_opening

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
        hives = _by_origin(hives, config.repo)
        role = ROLE_PROFILES.get(decision.profile, decision.profile)
        hives = [hive for hive in hives if placement.hosts(lane, hive, (decision.profile,))]
        harness = saved.get("harness") or chosen.get("agent") or "claude"
        if harness not in ("claude", "auto"):
            return self._record_wait(lane, task, placement.Wait("Codex hive placement is not enabled", role))
        rows = getattr(self.herdr, "_quota_accounts", None)
        rows = capacity.accounts(dict(os.environ), now / 1000, refresh=False) if rows is None else rows
        if lane == "master" and not task.get("route_kind"):
            front = front_door.holds(self.slug, task.get("seat", ""))
            selected = self._master_selection(config, decision, hives, now, rows, front)
            return self._record_wait(lane, task, selected)
        account = saved.get("account") or self.herdr.planned_account(task["id"], "claude")
        candidates = [
            row
            for row in rows
            if row.harness == "claude" and capacity.free_seats(row) and (not account or row.name == account)
        ]
        route_kind = task.get("route_kind", "subscription")
        found = _first_choice(
            candidates, lambda name: placement.choose(role, "claude", name, route_kind, config.repo, hives, now)
        )
        if not isinstance(found, placement.Wait):
            row, choice = found
            fallback = (
                f"the task is pinned to {task['hive']}" if choice.fallback and task.get("hive") else choice.fallback
            )
            matched = _matched_origin(hives, choice.hive, config.repo)
            found = (
                matched
                if isinstance(matched, placement.Wait)
                else Selection(decision, choice.hive, row, route_kind, fallback=fallback, origin=matched)
            )
        found = replace(found, role=role) if isinstance(found, placement.Wait) else found
        return self._record_wait(lane, task, found)

    def _master_selection(self, config, decision, hives, now, rows, front):
        accounts = {_key(row.name): row for row in rows if row.harness == "claude" and capacity.free_seats(row)}
        available = [
            {**hive, "slots": [slot for slot in hive["slots"] if _key(slot["account"]) in accounts]} for hive in hives
        ]
        selected = placement.place_master("claude", "", config.repo, available, now, front)
        if isinstance(selected, placement.Wait):
            return selected if hives else placement.Wait(f"no live hive takes {placement.MASTER}")
        if selected.route_kind == placement.MASTER_LOGIN:
            account = capacity.Account("claude", selected.account, "room", 0, None, None, kind=placement.MASTER_LOGIN)
        elif selected.route_kind == "interactive":
            account = capacity.Account("claude", selected.account, "room", 0, None, None, 1, kind="interactive")
        else:
            row = accounts[_key(selected.account)]
            account = replace(row, name=selected.account, cap=_slot_cap(available, selected, row.cap))
        matched = _matched_origin(hives, selected.hive, config.repo)
        if isinstance(matched, placement.Wait):
            return matched
        return Selection(decision, selected.hive, account, selected.route_kind, selected, origin=matched)

    def orders_for(self, task: str) -> LaunchOrder | None:
        raw = self.store.redis.hget(self.store.key(self.slug, ORDERS), task)
        return LaunchOrder(**json.loads(raw)) if raw else None

    def abandoned(self, order: LaunchOrder, previous: AgentRecord) -> bool:
        return lease.now_ms(self.store) >= order.expires_at and all(
            agent.name != previous.name for agent in self.store.agents(self.slug)
        )

    def unacknowledged(self, order: LaunchOrder) -> bool:
        return lease.now_ms(self.store) >= order.expires_at and self.orders.ack(order) is None

    def retire(self, order: LaunchOrder, previous: AgentRecord) -> None:
        self.reap(order, previous)
        self.end(previous, "hive launch order expired without an acknowledgement")

    def reap(self, order: LaunchOrder, agent: AgentRecord) -> None:
        self.orders.command(order.hive, CommandOrder(f"reap:{agent.name}", "reap", name=agent.name))

    def reap_execution(self, order: LaunchOrder) -> None:
        lease.require_epoch(self.store, self.slug, self.held.epoch)
        self.orders.command(
            order.hive,
            CommandOrder(
                f"reap:{order.slug}:{order.execution_id}:{order.generation}",
                "reap_execution",
                slug=order.slug,
                execution_id=order.execution_id,
                generation=order.generation,
            ),
        )

    def spawn(self, request: SpawnRequest) -> Outcome:
        lease.require_epoch(self.store, self.slug, request.task["controller_epoch"])
        if order := self.orders_for(request.task["id"]):
            previous = self.store.execution_registry.get(self.slug, order.execution_id)
            if previous.state in ("ended", "finished"):
                self.store.redis.hdel(self.store.key(self.slug, ORDERS), order.task)
            elif self.abandoned(order, previous):
                self.reap(order, previous)
                self.end(previous, "hive launch order abandoned")
            elif self.unacknowledged(order):
                self.retire(order, previous)
            elif previous.name != request.name or previous.seat != request.task["seat"]:
                return Outcome("spawn", Status.REFUSED, HIVE, detail="another claimant owns the hive launch order")
            else:
                return self.answer(request, order)
        selected = self.select(request.config, request.lane, request.task)
        if isinstance(selected, placement.Wait):
            return Outcome("spawn", Status.UNAVAILABLE, HIVE, detail=selected.reason)
        named = replace(request, config=replace(request.config, repo=selected.origin)) if selected.origin else request
        task = orders.carried(
            self.slug,
            {
                **request.task,
                "profile_decision": selected.profile.record(),
                **({orders.ORIGIN: selected.origin} if selected.origin else {}),
            },
            self.opening(named),
        )
        if refused := self.reserve_room(request, selected):
            self._record_wait(request.lane, request.task, placement.Wait(refused))
            return Outcome("spawn", Status.UNAVAILABLE, HIVE, detail=refused)
        try:
            return self.send_order(request, selected, task)
        except BaseException:
            admission.release(self.store.redis, selected.hive, f"{self.slug}/{request.name}")
            raise

    def send_order(self, request: SpawnRequest, selected: Selection, task: dict) -> Outcome:
        admitted = self.admit(request, selected)
        if selected.route_kind != placement.MASTER_LOGIN and not self.slots.reserve(
            f"claude:{selected.account.name}", admitted.execution_id, selected.account.cap, TTL_MS
        ):
            self.end(admitted, "account reservation is full")
            return Outcome("spawn", Status.UNAVAILABLE, HIVE, detail="account reservation is full")
        prompt_ref = self.store.key(self.slug, f"launch-prompt:{admitted.execution_id}")
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
            selected.hive,
        )
        with self.store.redis.pipeline() as pipe:
            pipe.set(prompt_ref, json.dumps(task), px=TTL_MS)
            pipe.hset(self.store.key(self.slug, ORDERS), request.task["id"], json.dumps(order.__dict__))
            Orders(pipe).send(selected.hive, order)
            pipe.execute()
        return self.answer(request, order)

    def collect(self, request: SpawnRequest) -> Outcome:
        order = self.orders_for(request.task["id"])
        previous = order and self.store.execution_registry.get(self.slug, order.execution_id)
        if not previous or previous.state in ("ended", "finished"):
            return Outcome("spawn", Status.REFUSED, HIVE, detail=GONE)
        if self.unacknowledged(order):
            self.retire(order, previous)
            return Outcome("spawn", Status.AMBIGUOUS, HIVE, detail=EXPIRED)
        return self.spawn(request)

    def expire(self, request: SpawnRequest) -> bool | None:
        """False when the hive acknowledged the order, which leaves its launch running for a later collect."""
        order = self.orders_for(request.task["id"])
        previous = order and self.store.execution_registry.get(self.slug, order.execution_id)
        if previous and previous.name == request.name and previous.state not in ("ended", "finished"):
            if self.orders.ack(order) is not None:
                return False
            self.retire(order, previous)

    def reserve_room(self, request: SpawnRequest, selected: Selection) -> str:
        if selected.master_placement is not None:
            return ""
        reading = headroom.read(self.store.redis, selected.hive, lease.now_ms(self.store))
        member = f"{self.slug}/{request.name}"
        reserved, held = admission.reserve(self.store.redis, selected.hive, member, admission.room(reading).room)
        if reserved:
            return ""
        refused = admission.refusal(admission.room(reading, pending=held))
        return f"no hive for {request.lane} has room: {selected.hive} {refused}"

    def admit(self, request: SpawnRequest, selected: Selection) -> AgentRecord:
        raw = self.store.redis.hget(self.store.key(self.slug, "agents"), request.name)
        started_at = json.loads(raw)["started_at"] if raw else lease.now_ms(self.store)
        agent = AgentRecord(
            request.name,
            request.lane,
            request.task["id"],
            seat=request.task["seat"],
            state="launching",
            started_at=started_at,
            profile=selected.profile.profile,
            profile_decision={
                **selected.profile.record(),
                **(
                    {"master_placement": {"hive": selected.hive, "headless": selected.master_placement.headless}}
                    if selected.master_placement is not None
                    else {}
                ),
                **({"placement_fallback": selected.fallback} if selected.fallback else {}),
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

    def answer(self, request: SpawnRequest, order: LaunchOrder) -> Outcome:
        return self.poll(order) if request.task.get(ACK_LATER) else self.wait(order)

    def wait(self, order: LaunchOrder) -> Outcome:
        while (outcome := self.poll(order)).status is Status.PENDING:
            self.sleep(1)
        return outcome

    def poll(self, order: LaunchOrder) -> Outcome:
        lease.require_epoch(self.store, self.slug, order.epoch)
        if ack := self.orders.ack(order):
            return self.complete(order, ack)
        if lease.now_ms(self.store) >= order.expires_at:
            release(self.store, self.slug, self.store.execution_registry.get(self.slug, order.execution_id))
            return Outcome("spawn", Status.AMBIGUOUS, HIVE, detail=EXPIRED)
        return Outcome("spawn", Status.PENDING, HIVE, detail=PENDING)

    def complete(self, order: LaunchOrder, ack: Ack) -> Outcome:
        agent = self.store.execution_registry.get(self.slug, order.execution_id)
        if ack.status != "live":
            if ack.failure in ("expired", orders.RESTARTED):
                self.reap(order, agent)
            if ack.failure != "ambiguous":
                self.end(agent, ack.failure)
                self.store.redis.hdel(self.store.key(self.slug, ORDERS), order.task)
            status = Status.AMBIGUOUS if ack.failure == "ambiguous" else Status.REFUSED
            return Outcome("spawn", status, HIVE, detail=f"hive launch failed: {ack.failure}")
        try:
            current = self.store.execution_occupants(self.slug).get(agent.seat)
            _match("seat occupant", order.execution_id, current.execution_id if current else None)
            _match("generation", order.generation, agent.generation)
            if agent.state == "launching":
                agent = self.record(agent, order, ack)
            _match("runtime target", agent.runtime_target, ack.target)
            _match("pane", agent.pane_id, ack.execution.get("pane_id", agent.pane_id))
            _match("state", "working", agent.state)
        except SwarmError:
            self.reap_execution(order)
            raise
        if agent.route_kind != placement.MASTER_LOGIN and not self.slots.acknowledge(
            agent.execution_id, f"claude:{agent.account}"
        ):
            self.reap(order, agent)
            self.end(agent, LATE)
            self.store.redis.hdel(self.store.key(self.slug, ORDERS), order.task)
            return Outcome("spawn", Status.REFUSED, HIVE, detail=LATE)
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
            launched_at=agent.launched_at,
            overlays=agent.overlays,
            launch_timings=agent.launch_timings,
        )
        return Outcome("spawn", Status.OK, HIVE, placed)

    def record(self, agent: AgentRecord, order: LaunchOrder, ack: Ack) -> AgentRecord:
        """A joined hive cannot write the swarm keyspace, so the controller records what it acknowledged."""
        decision = ack.execution.get("profile_decision", {})
        _match("ack host", order.hive, ack.target.get("host_id"))
        _match("profile decision type", "dict", type(decision).__name__)
        kept = (
            {"master_placement": agent.profile_decision["master_placement"]}
            if "master_placement" in agent.profile_decision
            else {}
        )
        live = replace(
            agent,
            runtime_target=ack.target,
            state="working",
            pane_id=ack.execution.get("pane_id", agent.pane_id),
            model=ack.execution.get("model", agent.model),
            effort=ack.execution.get("effort", agent.effort),
            profile_decision={**decision, **kept},
            launched_at=ack.execution.get("launched_at", agent.launched_at),
            launch_timings=ack.execution.get("launch_timings", agent.launch_timings),
            overlays=ack.execution.get("overlays", agent.overlays),
        )
        lease.require_epoch(self.store, self.slug, self.held.epoch)
        self.store.execution_registry.update(self.slug, live)
        return live

    def end(self, agent: AgentRecord, reason: str) -> None:
        lease.require_epoch(self.store, self.slug, self.held.epoch)
        release(self.store, self.slug, agent)
        self.store.redis.hset(
            self.store.key(self.slug, ENDINGS),
            agent.execution_id,
            json.dumps({"reason": reason, "generation": agent.generation}),
        )
        self.store.execution_registry.update(self.slug, replace(agent, state="ended"))
