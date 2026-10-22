import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING

from scripts import serena_router_daemon
from scripts.hive import registry
from scripts.swarm import launch_check, lease, prompt, reaper
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmError
from scripts.swarm_ledger import ledger_workspace
from scripts.swarm_v2.runtime.base import Outcome, SpawnRequest, Status
from scripts.swarm_v2.runtime.process import launch_target

if TYPE_CHECKING:
    from redis import Redis

    from scripts.swarm_v2.runtime.local import LocalHerdrRuntime

RECEIPT_SECONDS = 86400


@dataclass(frozen=True)
class LaunchOrder:
    slug: str
    execution_id: str
    generation: int
    profile: str
    lane: str
    task: str
    slot: str
    harness: str
    route_kind: str
    prompt_ref: str
    epoch: int
    expires_at: int
    hive: str = ""


@dataclass(frozen=True)
class Ack:
    status: str
    target: dict
    failure: str = ""
    execution: dict = field(default_factory=dict)

    def encode(self) -> str:
        fields = asdict(self)
        if not self.execution:
            del fields["execution"]
        return json.dumps(fields)


@dataclass(frozen=True)
class CommandOrder:
    id: str
    action: str
    slug: str = ""
    name: str = ""
    execution_id: str = ""
    generation: int = 0
    text: str = ""


DONE, FAILED = "done", "failed"
REFUSED = Ack(FAILED, {}, "refused")
FOREIGN = Ack(FAILED, {}, "identity")
UNKNOWN_ACTION = Ack(FAILED, {}, "unknown_action")
COMMAND_BATCH = 32
SENT = {"sent": "1"}
OPENING, DIGEST, SEEDS = prompt.CARRIED, "hive_opening_digest", "hive_seeds"
ORIGIN = "hive_origin"


def build_opening(request: SpawnRequest) -> str:
    """Built where the ledger is, naming the work folder below whichever home the hive runs it in."""
    config, task = request.config, {**request.task, "harness": "claude"}
    if task.get("workspace"):
        task["workspace"] = ledger_workspace.portable(config.slug, task["id"])
    role = config.lanes.get(request.lane, {}).get("role")
    return prompt.build(config.slug, config.repo, request.lane, request.name, task, role=role, autonomy=config.autonomy)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def carried(slug: str, task: dict, text: str) -> dict:
    sent = ledger_workspace.seeds(slug, task["id"]) if task.get("workspace") else {}
    return {**task, OPENING: text, DIGEST: digest(text), SEEDS: sent}


class Orders:
    def __init__(self, redis: "Redis") -> None:
        self.store = RedisStore(redis)
        self.redis = redis

    def stream(self, hive: str) -> str:
        return f"{registry.key(hive)}:orders"

    def receipt(self, order: LaunchOrder) -> str:
        return self.launch_receipt(order.hive, order.slug, order.execution_id, order.generation)

    def launch_receipt(self, hive: str, slug: str, execution_id: str, generation: int) -> str:
        return f"{registry.key(hive)}:launch-orders:{slug}:{execution_id}:{generation}"

    def send(self, hive: str, order: LaunchOrder) -> str:
        return self.redis.xadd(self.stream(hive), {"order": json.dumps(asdict(order))})

    def ack(self, order: LaunchOrder) -> Ack | None:
        raw = self.redis.hget(self.receipt(order), "ack")
        return Ack(**json.loads(raw)) if raw else None

    def finish(self, hive: str, item: str, order: LaunchOrder, ack: Ack) -> None:
        self.redis.hsetnx(self.receipt(order), "ack", ack.encode())
        canonical = self.redis.hget(self.receipt(order), "ack")
        with self.redis.pipeline() as pipe:
            pipe.hset(f"{self.stream(hive)}:acks", item, canonical)
            pipe.xdel(self.stream(hive), item)
            pipe.execute()

    def commands(self, hive: str) -> str:
        return f"{registry.key(hive)}:commands"

    def command_receipt(self, hive: str, order_id: str) -> str:
        return f"{registry.key(hive)}:command:{order_id}"

    def command(self, hive: str, order: CommandOrder) -> Ack | None:
        from redis.exceptions import WatchError

        receipt = self.command_receipt(hive, order.id)
        raw = self.redis.hget(receipt, "ack")
        if raw:
            return Ack(**json.loads(raw))
        with self.redis.pipeline() as pipe:
            try:
                pipe.watch(receipt)
                if pipe.hexists(receipt, "sent"):
                    return None
                pipe.multi()
                pipe.hset(receipt, mapping=SENT)
                pipe.expire(receipt, RECEIPT_SECONDS)
                pipe.xadd(self.commands(hive), {"order": json.dumps(asdict(order))})
                pipe.execute()
            except WatchError:
                return None
        return None

    def forget(self, hive: str, order_id: str) -> None:
        self.redis.delete(self.command_receipt(hive, order_id))


class Consumer:
    def __init__(self, redis: "Redis", hive: str, runtime: "LocalHerdrRuntime") -> None:
        self.store = RedisStore(redis)
        self.orders = Orders(redis)
        self.hive, self.runtime = hive, runtime

    @classmethod
    def local(cls, redis: "Redis", hive: str) -> "Consumer":
        from scripts.swarm.runtime import HerdrRuntime
        from scripts.swarm_v2.runtime.local import LocalHerdrRuntime

        return cls(redis, hive, LocalHerdrRuntime(HerdrRuntime()))

    def poll(self) -> int:
        from redis.exceptions import NoPermissionError

        entries = self.store.redis.xrange(self.orders.stream(self.hive), count=32)
        for item, fields in entries:
            try:
                order = LaunchOrder(**json.loads(fields["order"]))
                if order.hive != self.hive:
                    self._drop(item, "identity")
                    continue
                ack = self.orders.ack(order)
                if ack is None:
                    ack = self.apply(order)
                if ack is not None:
                    self.orders.finish(self.hive, item, order, ack)
            except (KeyError, TypeError, ValueError):
                self._drop(item, "invalid_order")
            except NoPermissionError:
                self._drop(item, "refused")
        return len(entries)

    def _drop(self, item: str, failure: str) -> None:
        with self.store.redis.pipeline() as pipe:
            pipe.hset(f"{self.orders.stream(self.hive)}:acks", item, Ack("failed", {}, failure).encode())
            pipe.xdel(self.orders.stream(self.hive), item)
            pipe.execute()

    def poll_commands(self) -> int:
        stream = self.orders.commands(self.hive)
        entries = self.store.redis.xrange(stream, count=COMMAND_BATCH)
        for item, fields in entries:
            try:
                order = CommandOrder(**json.loads(fields["order"]))
            except (KeyError, TypeError, ValueError):
                self.store.redis.xdel(stream, item)
                continue
            receipt = self.orders.command_receipt(self.hive, order.id)
            with self.store.redis.pipeline() as pipe:
                if not self.store.redis.hexists(receipt, "ack"):
                    try:
                        ack = self.command(order)
                    except Exception as exc:
                        ack = Ack(FAILED, {}, f"error: {type(exc).__name__}")
                    pipe.hset(receipt, "ack", ack.encode())
                    pipe.expire(receipt, RECEIPT_SECONDS)
                pipe.xdel(stream, item)
                pipe.execute()
        return len(entries)

    def sync_workspaces(self, ttl: int) -> None:
        hosted = [
            (slug, task)
            for slug in self.store.slugs()
            for task, agent in ledger_workspace.latest(self.store.agents(slug)).items()
            if agent.execution_id and agent.lane != MASTER and agent.runtime_target.get("host_id") == self.hive
        ]
        ledger_workspace.sync(self.store.redis, self.hive, hosted, ttl)

    def command(self, order: CommandOrder) -> Ack:
        if order.action == "reap":
            return _done(self.runtime.herdr.reap_name(order.name))
        try:
            agent = self.store.execution_registry.get(order.slug, order.execution_id)
        except SwarmError:
            return FOREIGN
        if agent.generation != order.generation or agent.runtime_target.get("host_id") != self.hive:
            return FOREIGN
        if order.action == "reap_execution":
            return self.reap_execution(order, agent)
        if order.action == "name":
            return _done(self.runtime.herdr.name_pane(agent))
        if order.action == "retire":
            outcome = self.runtime.terminate(agent, tuple(reaper.scratch_homes(order.slug, agent.task)))
        elif order.action == "nudge":
            outcome = self.runtime.command(agent, order.text)
        else:
            return UNKNOWN_ACTION
        if outcome.ok:
            return Ack(DONE, agent.runtime_target)
        return Ack(FAILED, agent.runtime_target, outcome.detail or outcome.status.value)

    def reap_execution(self, order: CommandOrder, agent: AgentRecord) -> Ack:
        receipt = self.orders.launch_receipt(self.hive, order.slug, order.execution_id, order.generation)
        raw = self.store.redis.hget(receipt, "ack")
        if raw is None:
            return FOREIGN
        ack = Ack(**json.loads(raw))
        if ack.status != "live" or ack.target.get("host_id") != self.hive:
            return FOREIGN
        live = replace(agent, runtime_target=ack.target, pane_id=ack.execution["pane_id"])
        outcome = self.runtime.terminate(live)
        return Ack(DONE if outcome.ok else FAILED, ack.target, "" if outcome.ok else outcome.detail)

    def apply(self, order: LaunchOrder) -> Ack | None:
        now = lease.now_ms(self.store)
        started = self.store.redis.hget(self.orders.receipt(order), "started")
        if started is not None:
            if order.expires_at <= now:
                return Ack("failed", {}, "expired")
            return Ack("failed", {}, "ambiguous") if now - int(started) >= 300000 else None
        if not self.store.redis.hsetnx(self.orders.receipt(order), "started", str(now)):
            return None
        if order.expires_at <= now:
            return Ack("failed", {}, "expired")
        try:
            lease.require_epoch(self.store, order.slug, order.epoch)
        except SwarmError:
            return Ack("failed", {}, "stale_epoch")
        from redis.exceptions import NoPermissionError

        try:
            agent = self.store.execution_registry.get(order.slug, order.execution_id)
            self.identity(order, agent)
        except (SwarmError, NoPermissionError):
            return Ack("failed", {}, "identity")
        raw = self.store.redis.get(order.prompt_ref)
        if raw is None:
            return Ack("failed", {}, "prompt")
        try:
            task = json.loads(raw)
        except ValueError:
            return Ack("failed", {}, "prompt")
        if not isinstance(task, dict) or task.get("id") != order.task:
            return Ack("failed", {}, "prompt")
        if not isinstance(task.get(OPENING), str) or task.get(DIGEST) != digest(task[OPENING]):
            return Ack("failed", {}, "prompt")
        try:
            return self.launch(order, agent, task)
        except SwarmError:
            return Ack("failed", {}, "refused")

    def identity(self, order: LaunchOrder, agent: AgentRecord) -> None:
        if (
            agent.generation != order.generation
            or agent.lane != order.lane
            or agent.task != order.task
            or agent.harness != order.harness
            or agent.profile != order.profile
            or agent.account != order.slot
            or agent.route_kind != order.route_kind
            or agent.runtime_backend != "local"
            or agent.runtime_target != {"host_id": self.hive}
            or agent.state != "launching"
        ):
            raise SwarmError("launch order does not match the admitted execution")
        current = self.store.execution_registry.occupants(order.slug).get(agent.seat)
        if current is None or current.execution_id != agent.execution_id:
            raise SwarmError("launch order names a replaced execution")

    def front_door_refusal(self, order: LaunchOrder, agent: AgentRecord) -> str:
        from scripts import master_claude_login
        from scripts.swarm_v2 import front_door

        if order.lane != MASTER or not front_door.holds(order.slug, agent.seat):
            return ""
        if (
            order.route_kind != master_claude_login.ROUTE
            and self.store.redis.hget(registry.key(self.hive), "ui") == "yes"
        ):
            return ""
        return master_claude_login.unusable()

    def launch(self, order: LaunchOrder, agent: AgentRecord, task: dict) -> Ack:
        if refused := self.front_door_refusal(order, agent):
            return Ack("failed", {}, refused)
        config = self.store.config(order.slug)
        chosen = {**config.lanes.get(order.lane, {}), "agent": order.harness, "role": order.profile}
        config = replace(config, repo=task.get(ORIGIN) or config.repo, lanes={**config.lanes, order.lane: chosen})
        task = {**task, "route_kind": order.route_kind, "controller_epoch": order.epoch}
        task["launch_assignment"] = {
            "profile": order.profile,
            "harness": order.harness,
            "account": order.slot,
            "model": agent.model or config.lanes[order.lane].get("model") or getattr(config, f"{order.harness}_model"),
            "effort": agent.effort or config.effort_min,
        }
        lease.require_epoch(self.store, order.slug, order.epoch)
        if task.get("workspace"):
            task["workspace"] = str(ledger_workspace.materialize(order.slug, order.task, task.get(SEEDS) or {}))
        if not serena_router_daemon.ensure():
            return Ack("failed", {}, "serena")
        outcome = self.runtime.spawn(SpawnRequest(config, order.lane, agent.name, task))
        if not outcome.ok:
            return Ack("failed", {}, _failure(outcome))
        placed = outcome.value
        pid = placed.profile_decision.get("validation", {}).get("pid")
        try:
            target = {"host_id": self.hive, **launch_target(pid, self.runtime.namespace(), self.runtime.table())}
        except SwarmError:
            return Ack("failed", {"host_id": self.hive}, "ambiguous")
        live = replace(agent, runtime_target=target, pane_id=placed.pane_id)
        if order.expires_at <= lease.now_ms(self.store):
            return self._stop(live, "expired")
        if (placed.harness, placed.profile, placed.account) != (order.harness, order.profile, order.slot):
            return self._stop(live, "identity")
        try:
            lease.require_epoch(self.store, order.slug, order.epoch)
        except SwarmError:
            return self._stop(live, "refused")
        execution = {
            "pane_id": placed.pane_id,
            "model": placed.model,
            "effort": placed.effort,
            "profile_decision": {
                **placed.profile_decision,
                launch_check.HOST_OVERLAYS: launch_check.declared(order.profile, placed.overlays),
            },
            "launched_at": placed.launched_at,
            "launch_timings": placed.launch_timings,
            "overlays": list(placed.overlays),
        }
        return Ack("live", target, "", execution)

    def _stop(self, agent: AgentRecord, failure: str) -> Ack:
        outcome = self.runtime.terminate(agent)
        return Ack("failed", agent.runtime_target, failure if outcome.ok else "ambiguous")


def _done(ok: bool) -> Ack:
    return Ack(DONE, {}) if ok else REFUSED


def _failure(outcome: Outcome) -> str:
    if outcome.status is Status.AMBIGUOUS or not outcome.detail:
        return outcome.status.value
    return f"{outcome.status.value}: {outcome.detail}"
