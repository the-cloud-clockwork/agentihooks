import hashlib
import json
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any

from scripts import serena_router_daemon
from scripts.hive import placement, registry
from scripts.scheduler.process_interval import Interval
from scripts.swarm import launch_check, lease, overlays, prompt, reaper
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmError
from scripts.swarm_ledger import ledger_workspace
from scripts.swarm_v2.runtime.base import Outcome, Recovery, SpawnRequest, Status
from scripts.swarm_v2.runtime.process import launch_target

if TYPE_CHECKING:
    from redis import Redis

    from scripts.swarm_v2.runtime.local import LocalHerdrRuntime

RECEIPT_SECONDS = 86400
RESUME_RUN_SECONDS = 120
RESUME_RENEW_SECONDS = 30
RESUME_RUNS = 3
RESUME_DEADLINE_MS = 1_800_000


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
    execution: dict[str, Any] = field(default_factory=dict)

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
    launched_at: int = 0
    pane_id: str = ""

    def encode(self) -> str:
        """The order as JSON, leaving out the judged launch and pane while unset so a hive on an older release still
        parses every order that carries neither."""
        fields = asdict(self)
        for name in ("launched_at", "pane_id"):
            if not fields[name]:
                del fields[name]
        return json.dumps(fields)


def answer(redis: Any, receipt: str, ack: "Ack") -> None:
    """Store a command order's acknowledgement unless one is already stored, so a late answer never overwrites it."""
    redis.hsetnx(receipt, "ack", ack.encode())
    redis.expire(receipt, RECEIPT_SECONDS)


DONE, FAILED = "done", "failed"
REFUSED = Ack(FAILED, {}, "refused")
FOREIGN = Ack(FAILED, {}, "identity")
UNKNOWN_ACTION = Ack(FAILED, {}, "unknown_action")
COMMAND_BATCH = 32
SENT = {"sent": "1"}
OPENING, DIGEST, SEEDS = prompt.CARRIED, "hive_opening_digest", "hive_seeds"
ORIGIN = "hive_origin"
RESTARTED = "restarted"
STALE_EPOCH = "stale_epoch"
POD = uuid.uuid4().hex
POD_SECONDS = 45
ACK_MARGIN_MS = 30000


def build_opening(request: SpawnRequest) -> str:
    """Built where the ledger is, naming the work folder below whichever home the hive runs it in."""
    config, task = request.config, {"harness": "claude", **request.task}
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

    def pod(self, hive: str, pod: str) -> str:
        return f"{registry.key(hive)}:pod:{pod}"

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
                pipe.xadd(self.commands(hive), {"order": order.encode()})
                pipe.execute()
            except WatchError:
                return None
        return None

    def forget(self, hive: str, order_id: str) -> None:
        self.redis.delete(self.command_receipt(hive, order_id))


def resolved_master_login(order: LaunchOrder, account: str, placement: str, decision: dict) -> bool:
    validation = decision.get("validation", {})
    return (
        order.route_kind == "master-login"
        and order.lane == MASTER
        and order.harness == "claude"
        and bool(account)
        and placement in ("forced", "fallback")
        and isinstance(validation, dict)
        and validation.get("account") == account
    )


class Consumer:
    def __init__(self, redis: "Redis", hive: str, runtime: "LocalHerdrRuntime", pod: str = POD) -> None:
        self.store = RedisStore(redis)
        self.orders = Orders(redis)
        self.hive, self.runtime, self.pod = hive, runtime, pod
        self.offbeat: Callable[[CommandOrder], None] = self._spawn_resume

    @classmethod
    def local(cls, redis: "Redis", hive: str) -> "Consumer":
        from scripts.swarm.runtime import HerdrRuntime
        from scripts.swarm_v2.runtime.local import LocalHerdrRuntime

        return cls(redis, hive, LocalHerdrRuntime(HerdrRuntime()))

    def poll(self) -> int:
        entries = self.store.redis.xrange(self.orders.stream(self.hive), count=32)
        for item, fields in entries:
            self.consume(item, fields)
        return len(entries)

    def consume(self, item: str, fields: dict) -> None:
        from redis.exceptions import NoPermissionError

        try:
            order = LaunchOrder(**json.loads(fields["order"]))
            if order.hive != self.hive:
                self._drop(item, "identity")
                return
            ack = self.orders.ack(order)
            if ack is None:
                ack = self.apply(order)
            if ack is not None:
                self.orders.finish(self.hive, item, order, ack)
        except (KeyError, TypeError, ValueError):
            self._drop(item, "invalid_order")
        except NoPermissionError:
            self._drop(item, "refused")

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
            redis: Any = self.orders.redis
            if order.action == "resume" and not redis.hexists(receipt, "ack"):
                self._start_resume(order, receipt)
                continue
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

    def _start_resume(self, order: CommandOrder, receipt: str) -> None:
        """Start a process for an unanswered resume order unless one holds its running marker; after RESUME_RUNS
        starts that never answered, answer it failed so the hub hands the task to a fresh agent."""
        redis: Any = self.orders.redis
        if not redis.set(f"{receipt}:running", "1", nx=True, ex=RESUME_RUN_SECONDS):
            return
        if redis.hincrby(receipt, "runs", 1) > RESUME_RUNS:
            answer(redis, receipt, Ack(FAILED, {"host_id": self.hive}, "resume never answered"))
            return
        redis.expire(receipt, RECEIPT_SECONDS)
        self.offbeat(order)

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
        if order.action == "reap_execution":
            return self.reap_execution(order, agent)
        if order.action == "resume":
            return self.resume(order, agent)
        if agent.generation != order.generation or agent.runtime_target.get("host_id") != self.hive:
            return FOREIGN
        if order.action == "name":
            return _done(self.runtime.herdr.name_pane(agent))
        if order.action == "retire":
            receipt = self.orders.launch_receipt(self.hive, order.slug, order.execution_id, order.generation)
            with self.store.redis.pipeline() as pipe:
                pipe.hset(receipt, "retired", "1")
                pipe.expire(receipt, RECEIPT_SECONDS)
                pipe.execute()
            outcome = self.runtime.terminate(agent, tuple(reaper.scratch_homes(order.slug, agent.task)))
        elif order.action == "nudge":
            outcome = self.runtime.command(agent, order.text)
        else:
            return UNKNOWN_ACTION
        if outcome.ok:
            return Ack(DONE, agent.runtime_target)
        return Ack(FAILED, agent.runtime_target, outcome.detail or outcome.status.value)

    def resume(self, order: CommandOrder, agent: AgentRecord) -> Ack:
        """Reopen a dead agent's own conversation in its seat while its execution still carries the launch and pane the
        hub judged and it still holds its task claim, then record the new pane; the hub checked the ledger claim,
        generation and seat first."""
        runtime: Any = self.runtime
        store: Any = self.store
        held = self._holding(order)
        if held is None:
            return FOREIGN
        reaped = runtime.reap(held.name)
        if not reaped.ok:
            return Ack(FAILED, {"host_id": self.hive}, reaped.detail or reaped.status.value)
        config = store.config(order.slug)
        if config.repo_origin:
            config = replace(config, repo=config.repo_origin)
        outcome = runtime.recover(held, Recovery.RESUME, config, order.text)
        if not outcome.ok:
            return Ack(FAILED, {"host_id": self.hive}, outcome.detail or outcome.status.value)
        placed = outcome.value
        current: Any = self._holding(order)
        if current is None:
            runtime.terminate(replace(held, pane_id=placed.pane_id))
            return FOREIGN
        launched = placed.launched_at or lease.now_ms(store)
        try:
            store.put_agent(order.slug, replace(current, pane_id=placed.pane_id, launched_at=launched, idle_ticks=0))
        except Exception:
            runtime.terminate(replace(held, pane_id=placed.pane_id))
            raise
        return Ack(DONE, {"host_id": self.hive})

    def run_resume(self, order: CommandOrder) -> None:
        """Answer one resume order off the hive heartbeat, renewing its running marker while it works so no second
        process starts it, and store the first acknowledgement only."""
        receipt = self.orders.command_receipt(self.hive, order.id)
        redis: Any = self.orders.redis
        renewal = Interval(lambda: redis.expire(f"{receipt}:running", RESUME_RUN_SECONDS), every=RESUME_RENEW_SECONDS)
        renewal.start()
        try:
            ack = self.command(order)
        except Exception as exc:
            ack = Ack(FAILED, {}, f"error: {type(exc).__name__}")
        finally:
            renewal.close()
        answer(redis, receipt, ack)

    def _spawn_resume(self, order: CommandOrder) -> None:
        """Run a resume order in its own process so a slow herdr resume never holds the heartbeat."""
        subprocess.Popen(
            [sys.executable, "-m", "scripts.hive.resume_order", self.hive, order.encode()],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )

    def _holding(self, order: CommandOrder) -> AgentRecord | None:
        """The order's execution record while it runs on this hive with the generation, launch and pane the hub judged
        and its agent still holds its task claim, else None."""
        try:
            current = self.store.execution_registry.get(order.slug, order.execution_id)
        except SwarmError:
            return None
        store: Any = self.store
        record: Any = current
        target: dict[str, Any] = record.runtime_target
        judged = (order.name, order.generation, order.launched_at, order.pane_id, self.hive)
        host = target.get("host_id") or current.hive
        found = (current.name, current.generation, current.launched_at, current.pane_id, host)
        return current if found == judged and store.claimant(order.slug, current.task) == order.name else None

    def reap_execution(self, order: CommandOrder, agent: AgentRecord) -> Ack:
        receipt = self.orders.launch_receipt(self.hive, order.slug, order.execution_id, order.generation)
        raw = self.store.redis.hget(receipt, "ack")
        if raw is None:
            return FOREIGN
        ack = Ack(**json.loads(raw))
        if ack.status != "live" or ack.target.get("host_id") != self.hive:
            return FOREIGN
        live = replace(agent, generation=order.generation, runtime_target=ack.target, pane_id=ack.execution["pane_id"])
        outcome = self.runtime.terminate(live)
        return Ack(DONE if outcome.ok else FAILED, ack.target, "" if outcome.ok else outcome.detail)

    def apply(self, order: LaunchOrder) -> Ack | None:
        now = lease.now_ms(self.store)
        started = self.store.redis.hget(self.orders.receipt(order), "started")
        if started is not None:
            return self.resumed(order, int(started), now)
        with self.store.redis.pipeline() as pipe:
            pipe.hsetnx(self.orders.receipt(order), "started", str(now))
            pipe.hsetnx(self.orders.receipt(order), "pod", self.pod)
            if not pipe.execute()[0]:
                return None
        if order.expires_at - ACK_MARGIN_MS <= now:
            return Ack("failed", {}, "expired")
        try:
            lease.require_epoch(self.store, order.slug, order.epoch)
        except SwarmError:
            return Ack("failed", {}, STALE_EPOCH)
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

    def resumed(self, order: LaunchOrder, started: int, now: int) -> Ack | None:
        if order.expires_at <= now:
            return Ack("failed", {}, "expired")
        if self.orphaned(order):
            return Ack("failed", {}, RESTARTED)
        return Ack("failed", {}, "ambiguous") if now - started >= 300000 else None

    def orphaned(self, order: LaunchOrder) -> bool:
        owner = self.store.redis.hget(self.orders.receipt(order), "pod")
        return owner not in (None, self.pod) and not self.store.redis.exists(self.orders.pod(self.hive, owner))

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
            and (registry.show(self.store.redis, self.hive) or {}).get("ui") == "yes"
        ):
            return ""
        return master_claude_login.unusable()

    def slot_account(self, order: LaunchOrder) -> str:
        """The hive's own advertised name for the order's account, which the agent launcher looks up."""
        key = placement.account_key(order.slot)
        hive: dict[str, list[dict[str, str]]] = registry.show(self.orders.redis, self.hive) or {}  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        for slot in hive.get("slots", []):
            if slot["harness"] == order.harness and placement.account_key(slot["account"]) == key:
                return slot["account"]
        return order.slot

    def launch(self, order: LaunchOrder, agent: AgentRecord, task: dict[str, Any]) -> Ack:
        if refused := self.front_door_refusal(order, agent):
            return Ack("failed", {}, refused)
        config = self.store.config(order.slug)
        chosen = {**config.lanes.get(order.lane, {}), "agent": order.harness, "role": order.profile}
        config = replace(config, repo=task.get(ORIGIN) or config.repo, lanes={**config.lanes, order.lane: chosen})
        task = {
            **task,
            "route_kind": order.route_kind,
            "controller_epoch": order.epoch,
            "admitted_execution_id": agent.execution_id,
            "launch_until_ms": int(time.time() * 1000) + order.expires_at - ACK_MARGIN_MS - lease.now_ms(self.store),
        }
        try:
            worn = overlays.chosen(order.profile, task, config.overlays, ())
        except ValueError as exc:
            return Ack("failed", {}, f"overlays: {exc}")
        task["launch_assignment"] = {
            "profile": order.profile,
            "harness": order.harness,
            "account": self.slot_account(order),
            "model": agent.model or config.lanes[order.lane].get("model") or getattr(config, f"{order.harness}_model"),
            "effort": agent.effort or config.effort_min,
            "overlays": list(worn),
        }
        lease.require_epoch(self.store, order.slug, order.epoch)
        if task.get("workspace"):
            task["workspace"] = str(ledger_workspace.materialize(order.slug, order.task, task.get(SEEDS) or {}))
        if not serena_router_daemon.ensure():
            return Ack("failed", {}, "serena")
        if self.store.redis.hexists(self.orders.receipt(order), "retired"):
            return Ack("failed", {}, "retired")
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
        if self.store.redis.hexists(self.orders.receipt(order), "retired"):
            return self._stop(live, "retired")
        if order.expires_at <= lease.now_ms(self.store):
            return self._stop(live, "expired")
        resolved = resolved_master_login(order, placed.account, placed.placement, placed.profile_decision)
        if (placed.harness, placed.profile) != (order.harness, order.profile) or (
            placement.account_key(placed.account) != placement.account_key(order.slot) and not resolved
        ):
            return self._stop(live, "identity")
        try:
            lease.require_epoch(self.store, order.slug, order.epoch)
        except SwarmError:
            return self._stop(live, "refused")
        execution = {
            **({"account": placed.account, "placement": placed.placement} if resolved else {}),
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
            **({"remote_control": placed.remote_control} if placed.remote_control else {}),
        }
        return Ack("live", target, "", execution)

    def _stop(self, agent: AgentRecord, failure: str) -> Ack:
        outcome = self.runtime.terminate(agent)
        return Ack("failed", agent.runtime_target, failure if outcome.ok else "ambiguous")


def alive(redis: "Redis", hive: str, pod: str = POD) -> None:
    redis.set(Orders(redis).pod(hive, pod), "1", ex=POD_SECONDS)


def _done(ok: bool) -> Ack:
    return Ack(DONE, {}) if ok else REFUSED


def _failure(outcome: Outcome) -> str:
    if outcome.status is Status.AMBIGUOUS or not outcome.detail:
        return outcome.status.value
    return f"{outcome.status.value}: {outcome.detail}"
