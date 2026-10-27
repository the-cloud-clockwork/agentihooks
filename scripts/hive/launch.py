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


@dataclass(frozen=True)
class _Placing:
    config: SwarmConfig
    task: dict
    saved: dict
    decision: profile_choice.ProfileDecision
    role: str
    rows: list[capacity.Account]
    hives: list[dict]
    now: int


ROOM_DETAILS = {"reason": "", "detail": ""}


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
        from scripts.swarm import local_slots
        from scripts.swarm.commands import hive_id

        held = admission.pending(store.redis, hive["id"])
        used = hive["agents"]
        if hive["id"] == hive_id():
            running, taken = local_slots.occupancy(store, hive["id"])
            hive["agents"] = max(hive["agents"], running)
            used = max(hive["agents"], taken)
            held = max(held, used - hive["agents"])
        found = admission.capped(
            admission.room(headroom.read(store.redis, hive["id"], now), pending=held),
            used,
            hive["max_agents"],
        )
        hive["room"] = found.room
        hive["refusal"] = admission.refusal(found)
    return hives


def _key(account: str) -> str:
    """A hive names an account by its AH_CC_TOKEN_ slug, a hosted controller by its Secret key; only _ and - differ."""
    return account.replace("_", "-")


def _open_rows(rows: list, harness: str = "claude") -> list:
    return [row for row in rows if row.harness == harness and capacity.free_seats(row) > 0]


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


def _pinned(hives: list[dict], task: dict, role: str) -> tuple[list[dict], placement.Wait | None]:
    from scripts.swarm.commands import hive_id

    if task.get("hive"):
        hives = [hive for hive in hives if hive["id"] == task["hive"]]
    if task.get("needs"):
        equipped = [hive for hive in hives if placement.equipped(task, hive)]
        if not equipped:
            return equipped, placement.Wait(placement.needs_refusal(task, hives), role)
        hives = equipped
    if not task.get("workstation"):
        return hives, None
    kept = [hive for hive in hives if hive["id"] == hive_id()]
    return kept, None if kept else placement.Wait(placement.NEEDS_WORKSTATION, role)


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
    from scripts.swarm import local_slots

    LaunchSlots(store.redis).release(agent.execution_id)
    admission.release(store.redis, agent.hive, f"{slug}/{agent.name}")
    local_slots.release(store, agent.hive, slug, agent.name)


class HiveLaunch:
    backend = HIVE
    capabilities = frozenset((Capability.SPAWN,))

    def __init__(self, store: RedisStore, slug: str, held: lease.Lease, herdr: object) -> None:
        self.store, self.slug, self.held, self.herdr = store, slug, held, herdr
        self.orders, self.slots = Orders(store.redis), LaunchSlots(store.redis)
        self.sleep = time.sleep
        self.opening = orders.build_opening
        self.placement_cache: dict[tuple[str, str], Selection | placement.Wait] | None = None
        self._pass_hives: tuple[dict, list[dict]] | None = None
        self._quota_accounts = None

    def hives(self) -> list[dict]:
        if self.placement_cache is None:
            return placement.for_swarm(loaded_hives(self.store), self.slug)
        if self._pass_hives is None or self._pass_hives[0] is not self.placement_cache:
            self._pass_hives = (self.placement_cache, placement.for_swarm(loaded_hives(self.store), self.slug))
        return self._pass_hives[1]

    def _record_wait(self, lane: str, task: dict, selected: Selection | placement.Wait) -> Selection | placement.Wait:
        reason = selected.reason if isinstance(selected, placement.Wait) else ""
        from scripts.swarm.commands import hive_id

        machine = hive_id() if task.get("workstation") else ""
        placement_waits.observe(self.store, self.slug, lane, task["id"], reason, lease.now_ms(self.store), machine)
        return selected

    def select(self, config: SwarmConfig, lane: str, task: dict) -> Selection | placement.Wait:
        if self.placement_cache is None:
            return self._select(config, lane, task)
        key = (lane, task["id"])
        if key not in self.placement_cache:
            self.placement_cache[key] = self._spend(self._select(config, lane, task))
        return self.placement_cache[key]

    def _spend(self, selected: Selection | placement.Wait) -> Selection | placement.Wait:
        if isinstance(selected, Selection) and selected.master_placement is None:
            for hive in self.hives():
                if hive["id"] == selected.hive:
                    hive["room"] -= 1
        return selected

    def _select(self, config: SwarmConfig, lane: str, task: dict) -> Selection | placement.Wait:
        chosen = config.lanes.get(lane, {})
        saved = task.get("launch_assignment") or profile_choice.transfer(task)
        decision = self.herdr._profile_decision(config, lane, chosen, task, saved, saved, dict(os.environ))
        hives, now = self.hives(), lease.now_ms(self.store)
        role = ROLE_PROFILES.get(decision.profile, decision.profile)
        hives, refused = _pinned(_by_origin(hives, config.repo), task, role)
        if refused:
            return self._record_wait(lane, task, refused)
        hives = [hive for hive in hives if placement.hosts(lane, hive, (decision.profile,))]
        harness = saved.get("harness") or chosen.get("agent") or "auto"
        auto = harness == "auto"
        if auto:
            harness = getattr(self.herdr, "_quota_tasks", {}).get(task["id"]) or "claude"
        if harness not in ("claude", "codex"):
            return self._record_wait(lane, task, placement.Wait(f"unsupported hive harness: {harness}", role))
        rows = getattr(self.herdr, "_quota_accounts", None)
        if rows is None:
            if self._quota_accounts is None:
                self._quota_accounts = capacity.accounts(dict(os.environ), now / 1000, refresh=False)
            rows = self._quota_accounts
        if lane == "master" and not task.get("route_kind"):
            front = front_door.holds(self.slug, task.get("seat", ""))
            selected = self._master_selection(config, decision, hives, now, rows, front)
            return self._record_wait(lane, task, selected)
        from scripts.swarm import local_slots
        from scripts.swarm.commands import hive_id

        if lane != placement.MASTER:
            for index, hive in enumerate(hives):
                if hive["id"] == hive_id():
                    bound = local_slots.maximum(self.store, self.slug, hive["max_agents"])
                    taken = local_slots.held(self.store, hive["id"], self.slug, task["id"])
                    limited = admission.capped(admission.Room(hive["room"], **ROOM_DETAILS), taken, bound)
                    if limited.room < hive["room"]:
                        hives[index] = {**hive, "room": limited.room, "refusal": admission.refusal(limited)}
        placing = _Placing(config, task, saved, decision, role, rows, hives, now)
        found = self._account_selection(placing, harness)
        if auto and harness == "codex" and isinstance(found, placement.Wait):
            claude = self._account_selection(placing, "claude")
            found = claude if isinstance(claude, Selection) else found
        return self._record_wait(lane, task, found)

    def _account_selection(self, placing: _Placing, harness: str) -> Selection | placement.Wait:
        task, rows, role = placing.task, placing.rows, placing.role
        account = placing.saved.get("account") or self.herdr.planned_account(task["id"], harness)
        open_rows = _open_rows(rows, harness)
        candidates = [row for row in open_rows if not account or row.name == account]
        if not candidates:
            candidates = open_rows
        if not candidates:
            seats = ", ".join(
                f"{row.name} {row.sessions} sessions of {row.cap}" for row in rows if row.harness == harness
            )
            pinned = f"; pinned to {account}" if account else ""
            return placement.Wait(
                f"no {harness.capitalize()} account has room after hive sessions: {seats}{pinned}", role
            )
        route_kind = task.get("route_kind", "subscription")
        repo, hives = placing.config.repo, placing.hives
        found = _first_choice(
            candidates, lambda name: placement.choose(role, harness, name, route_kind, repo, hives, placing.now)
        )
        if isinstance(found, placement.Wait):
            return replace(found, role=role)
        row, choice = found
        fallback = f"the task is pinned to {task['hive']}" if choice.fallback and task.get("hive") else choice.fallback
        matched = _matched_origin(hives, choice.hive, repo)
        if isinstance(matched, placement.Wait):
            return replace(matched, role=role)
        return Selection(placing.decision, choice.hive, row, route_kind, fallback=fallback, origin=matched)

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
        named = replace(
            request,
            config=replace(request.config, repo=selected.origin) if selected.origin else request.config,
            task={**request.task, "harness": selected.account.harness},
        )
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
            from scripts.swarm import local_slots

            admission.release(self.store.redis, selected.hive, f"{self.slug}/{request.name}")
            local_slots.release(self.store, selected.hive, self.slug, request.name)
            raise

    def send_order(self, request: SpawnRequest, selected: Selection, task: dict) -> Outcome:
        from scripts.master_login_chain import BUDGET

        ttl = BUDGET.order_ttl_ms if selected.route_kind == placement.MASTER_LOGIN else TTL_MS
        admitted = self.admit(request, selected)
        if selected.route_kind != placement.MASTER_LOGIN:
            account = selected.account
            reading = self.slots.reserve_reading(
                f"{account.harness}:{account.name}", admitted.execution_id, account.cap, TTL_MS
            )
            if not reading.accepted:
                refused = (
                    f"account reservation is full: account={account.harness}:{account.name} "
                    f"sessions={reading.sessions} cap={account.cap} "
                    f"five_left={account.five_left} week_left={account.week_left}"
                )
                self.end(admitted, refused)
                return Outcome("spawn", Status.UNAVAILABLE, HIVE, {"harness": account.harness}, detail=refused)
        prompt_ref = self.store.key(self.slug, f"launch-prompt:{admitted.execution_id}")
        order = LaunchOrder(
            self.slug,
            admitted.execution_id,
            admitted.generation,
            admitted.profile,
            request.lane,
            request.task["id"],
            selected.account.name,
            selected.account.harness,
            selected.route_kind,
            prompt_ref,
            self.held.epoch,
            lease.now_ms(self.store) + ttl,
            selected.hive,
        )
        with self.store.redis.pipeline() as pipe:
            pipe.set(prompt_ref, json.dumps(task), px=ttl)
            pipe.hset(self.store.key(self.slug, ORDERS), request.task["id"], json.dumps(order.__dict__))
            Orders(pipe).send(selected.hive, order)
            pipe.execute()
        return self.answer(request, order)

    def collect(self, request: SpawnRequest) -> Outcome:
        order = self.orders_for(request.task["id"])
        previous = order and self.store.execution_registry.get(self.slug, order.execution_id)
        if previous and previous.state == "finished":
            return self.finished(request, order, previous)
        if not previous or previous.state in ("ended", "finished"):
            return Outcome("spawn", Status.REFUSED, HIVE, detail=GONE)
        if self.unacknowledged(order):
            self.retire(order, previous)
            return Outcome("spawn", Status.AMBIGUOUS, HIVE, detail=EXPIRED)
        return self.spawn(request)

    def finished(self, request: SpawnRequest, order: LaunchOrder, agent: AgentRecord) -> Outcome:
        lease.require_epoch(self.store, self.slug, request.task["controller_epoch"])
        lease.require_epoch(self.store, self.slug, order.epoch)
        _match("claimant", agent.name, request.name)
        _match("seat", agent.seat, request.task["seat"])
        _match("generation", order.generation, agent.generation)
        ack = self.orders.ack(order)
        if not ack or ack.status != "live":
            return Outcome("spawn", Status.REFUSED, HIVE, detail=GONE)
        _match("ack host", order.hive, ack.target.get("host_id"))
        if agent.runtime_target != ack.target:
            bound = replace(
                agent,
                runtime_target=ack.target,
                pane_id=ack.execution["pane_id"],
                launched_at=ack.execution["launched_at"],
                launch_timings=ack.execution["launch_timings"],
            )
            self.store.execution_registry.update(self.slug, bound)
        return Outcome("spawn", Status.OK, HIVE)

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
        from scripts.swarm import local_slots
        from scripts.swarm.commands import hive_id

        reading = headroom.read(self.store.redis, selected.hive, lease.now_ms(self.store))
        member = f"{self.slug}/{request.name}"
        local = selected.hive == hive_id() and request.lane != placement.MASTER
        if local:
            running = local_slots.running(self.store, self.slug, request.task["id"])
            bound = local_slots.maximum(self.store, self.slug, running + admission.room(reading).room)
            taken = local_slots.claim(self.store, selected.hive, self.slug, request.task["id"], request.name, bound)
            if taken >= bound:
                refused = f"no hive has room{placement.LOCAL_FULL}{taken} of its {bound} agents"
                return refused  # nosemgrep: python.flask.security.audit.directly-returned-format-string.directly-returned-format-string
        reserved, held = admission.reserve(self.store.redis, selected.hive, member, admission.room(reading).room)
        if reserved:
            return ""
        if local:
            local_slots.release(self.store, selected.hive, self.slug, request.name)
        refused = admission.refusal(admission.room(reading, pending=held))
        return f"no hive for {request.lane} {placement.HAS_ROOM}: {selected.hive} {refused}"

    def admit(self, request: SpawnRequest, selected: Selection) -> AgentRecord:
        from scripts.swarm_v2.runtime import codex

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
            harness=selected.account.harness,
            transport=codex.transport(request.config, selected.account.harness),
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
            agent.execution_id, f"{agent.harness}:{agent.account}"
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
        account = ack.execution.get("account", agent.account)
        if account != agent.account and not orders.resolved_master_login(
            order, account, ack.execution.get("placement", ""), decision
        ):
            _match("ack account", agent.account, account)
        kept = {
            key: agent.profile_decision[key]
            for key in ("master_placement", "placement_fallback")
            if key in agent.profile_decision
        }
        live = replace(
            agent,
            account=account,
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
