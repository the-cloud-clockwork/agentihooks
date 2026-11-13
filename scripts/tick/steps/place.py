"""The placement steps of the tick pass, run at their sites in today's order: acks, ranking and spawn."""

# pyright: reportUnusedFunction=false, reportUnusedClass=false

import json
import os
from collections.abc import Callable, Collection, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field, replace
from functools import partial
from importlib import import_module
from types import MappingProxyType
from typing import Any

from scripts.swarm import hash_pages, ledger_client, lost_runner, quota_notice, time_left
from scripts.swarm.naming import seat_label
from scripts.swarm.profile_choice import ProfileUnresolved
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.tick import engine, registry
from scripts.tick.adapters.calls import (
    AdmissionCalls,
    LedgerCalls,
    LocalMetrics,
    MetricsCalls,
    SpawnRuntimeCalls,
    SpawnStoreCalls,
)
from scripts.tick.deadline import Deadline
from scripts.tick.engine import SLOW
from scripts.tick.steps import AGENTS, DEADLINE, SWARM, TASKS, CallStep, budgets, pass_domains
from scripts.tick.steps.tasks import RANK, WriteStep, held, placing, rank_writes

transfers: Any = import_module("scripts.handoff.transfers")
Who: Any = import_module("scripts.gates").Who
modes: Any = import_module("scripts.gates.modes")
claim_cap: Any = import_module("scripts.gates.claims")
gate_log: Any = import_module("scripts.gates.log")
continuation: Any = import_module("scripts.swarm.continuation")
dispatcher: Any = import_module("scripts.swarm.dispatcher")
failed_panes: Any = import_module("scripts.swarm.failed_panes")
launch_check: Any = import_module("scripts.swarm.launch_check")
ledger_events: Any = import_module("scripts.swarm.ledger_events")
preempt: Any = import_module("scripts.swarm.preempt")
profile_first: Any = import_module("scripts.swarm.profile_first")
readiness: Any = import_module("scripts.swarm.readiness")
run_window: Any = import_module("scripts.swarm.run_window")
seat_spawn: Any = import_module("scripts.swarm.seat_spawn")
successor_retry: Any = import_module("scripts.swarm.successor_retry")
successor_fill: Any = import_module("scripts.swarm.successor_fill")
timing: Any = import_module("scripts.swarm.timing")
ledger_rank: Any = import_module("scripts.swarm_ledger.ledger_rank")
ledger_workspace: Any = import_module("scripts.swarm_ledger.ledger_workspace")

__all__ = ["ACKS", "NOTICES", "PLACED", "PLACEMENT", "STEPS"]

Get = Callable[[str], Any]
NOTICES = "placement notices"
ACKS = "placement acks"
PLACEMENT = "placement"
RESUMED = frozenset({"drained resume"})
CONFIG_DOC = pass_domains("config", "doc")
TASK_STATE = pass_domains("config", "rows", "doc")
PLACED = frozenset({AGENTS, TASKS})


_SPAWN_KEYS = ("slug", "config", "store", "ledger", "runtime", "rows", "doc", "now_ms")
PASS_DEADLINE: ContextVar[Deadline | None] = ContextVar("spawn_pass_deadline", default=None)
PASS_METRICS: ContextVar[MetricsCalls | None] = ContextVar("spawn_pass_metrics", default=None)
OFFLINE_HOLD = "the ledger is not answering, so its claim cannot land before the launch"


def _quota_notice(tick: Any, get: Get) -> Any:
    keys = ("slug", "config", "store", "ledger", "runtime", "now_ms")
    return tick.skip_refused(quota_notice.refresh, *map(get, keys))


def _lost_runner(tick: Any, get: Get) -> Any:
    keys = ("slug", "config", "store", "now_ms")
    return tick.skip_refused(lost_runner.sweep, *map(get, keys))  # pyright: ignore[reportUnknownMemberType]


def _time_left(tick: Any, get: Get) -> Any:
    keys = ("slug", "store", "ledger", "runtime", "config", "doc")
    return tick.skip_refused(time_left.refresh, *map(get, keys))  # pyright: ignore[reportUnknownMemberType]


def _codex_hook_order(tick: Any, get: Get) -> Any:
    if not get("sleeping"):
        return tick.skip_refused(tick._codex_hook_order)
    return []


def _rank_pass(tick: Any, get: Get) -> Any:
    keys = ("slug", "config", "store", "ledger", "doc", "now_ms")
    return tick.skip_refused(rank_writes, tick, *map(get, keys))


def _bottleneck_remedy(tick: Any, get: Get) -> Any:
    return tick.skip_refused(
        tick.bottleneck_remedy.step, get("slug"), get("store"), get("ledger"), get("doc"), get("now_ms")
    )


def _spawn_step(tick: Any, get: Get) -> Any:
    config, deadline, tools = get("config"), get(DEADLINE), get("tools")
    recording = _recording(tick, deadline)
    store = SpawnStoreCalls(tools.coordination, recording, tools.inbox)
    if config.state == "running" and tick.readiness.tick_launches(config.engine) and tick.panic.state(store) is None:
        rows = dict(get("rows"))
        runtime = SpawnRuntimeCalls(tools.runtime, recording)
        ledger = LedgerCalls(tools.ledger, recording)
        bound = {"rows": rows, "store": store, "ledger": ledger, "runtime": runtime}
        args = tuple(bound[key] if key in bound else get(key) for key in _SPAWN_KEYS)
        token = PASS_DEADLINE.set(deadline)
        ports = MetricsCalls(tools.metrics, tools.processes, recording)
        measured, logged = PASS_METRICS.set(ports), dispatcher.PORT.set(ports)
        try:
            with tick._placement_pass(runtime):
                found = tick.skip_refused(tick._spawn, *args)
        finally:
            dispatcher.PORT.reset(logged)
            PASS_METRICS.reset(measured)
            PASS_DEADLINE.reset(token)
        get("box")["rows"] = MappingProxyType(rows)
        store.redis.delete(get("due"))
        return found
    return []


def _recording(tick: Any, deadline: Deadline) -> Deadline:
    """Return ``deadline`` extended by the launch budget, so a task prepared before it is still recorded on the ledger."""
    return Deadline(deadline.expires_at + tick.SPAWN_BUDGET_S, deadline.clock)


@dataclass(frozen=True)
class Started:
    record: Any
    task: dict[str, Any]
    placed: Any
    launched: Any


def _started(
    slug: str, config: Any, store: Any, ledger: Any, doc: dict[str, Any], started: Started, now_ms: int
) -> list[str]:
    record, task, placed = started.record, started.task, started.placed
    store.count_claim(slug, task["id"])
    after = store.take_restart(slug, task["id"]) or tick._master_set(doc, task["id"])
    head = (ledger_events.head(task["pr_url"]) or None) if task.get("pr_url") else ""
    store.record_launch(slug, replace(record, pane_id=placed.pane_id), "started", after=after, head=head)
    store.put_agent(slug, started.launched)
    launch_check.begin(store, slug, record, now_ms)
    store.count_spawn(slug, placed.harness)
    tick._spend_local(store, started.launched, now_ms)
    store.clear_handoff(slug, task["id"])
    store.clear_failed_harness(slug, task["id"])
    store.redis.hdel(store.key(slug, "launch-assignments"), task["id"])
    profile_first.spawned(slug, store, task["id"])
    return [
        f"spawned {record.name} for {task['id']}",
        *dispatcher.chain_claimed(slug, config, ledger, doc, task, record.lane, now_ms),
    ]


@dataclass(frozen=True)
class Failed:
    record: Any
    task: dict[str, Any]
    error: Exception


def _spawn_failed(
    slug: str, store: Any, ledger: Any, runtime: Any, rows: dict[str, Any], failed: Failed, now_ms: int
) -> list[str]:
    record, task, exc = failed.record, failed.task, failed.error
    tick._release_admission(slug, runtime, task)
    tick._release_hive_launch(slug, store, record)
    tick._record_spawn_failure(slug, store, record, exc, now_ms)
    actions: list[str] = []
    if "account reservation is full" in str(exc):
        from scripts.swarm import capacity, quota_handoff

        decision: dict[str, Any] = capacity.read(store, slug)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        readings: list[Any] = decision.get("accounts", []) if decision.get("observed_at") == now_ms else []
        thresholds: Any = quota_handoff.Thresholds.from_env(dict(os.environ))  # pyright: ignore[reportUnknownMemberType]
        snapshot = {
            "harness": getattr(exc, "harness", ""),
            "accounts": [row for row in readings if not quota_handoff.trigger(capacity.Account(**row), thresholds)],
        }
        actions.append(f"spawn capacity for {task['id']}: {json.dumps(snapshot)}")
    actions.append(f"spawn failed for {task['id']}{tick._drop(slug, store, ledger, rows, record)}: {exc}")
    if isinstance(exc, ProfileUnresolved):
        actions.append(tick._unresolved(slug, ledger, rows, task["id"], str(exc)))
    return actions


def _not_launched(
    slug: str, store: Any, ledger: Any, runtime: Any, rows: dict[str, Any], failed: Failed, now_ms: int
) -> list[str]:
    record, task = failed.record, failed.task
    if tick._acknowledging_error(failed.error):
        store.record_launch(slug, record, tick.ACKNOWLEDGING)
        return [f"{record.name} waits for the hive acknowledgement of {task['id']}"]
    if refused := tick._failed_room_wait(runtime, record.lane, task) or tick._refused_reservation(task, failed.error):
        return [tick._room_refused(slug, store, ledger, runtime, rows, failed, refused)]
    return tick._spawn_failed(slug, store, ledger, runtime, rows, failed, now_ms)


def _failed_room_wait(runtime: Any, lane: str, task: dict[str, Any]) -> str | bool:
    try:
        return tick._room_wait(runtime, lane, task)
    except tick.SpawnError:
        return False


def _refused_reservation(task: dict[str, Any], exc: Exception) -> str:
    return f"task {task['id']} waits: {exc}" if seat_spawn.room_refused(exc) else ""


def _room_refused(
    slug: str, store: Any, ledger: Any, runtime: Any, rows: dict[str, Any], failed: Failed, refused: str
) -> str:
    record, task = failed.record, failed.task
    tick._release_admission(slug, runtime, task)
    tick._release_hive_launch(slug, store, record)
    transfers.detach(store, slug, record)
    store.record_launch(slug, record, "waiting", str(failed.error))
    tick._drop(slug, store, ledger, rows, record)
    return refused


def _room_wait(runtime: Any, lane: str, task: dict[str, Any]) -> str:
    try:
        refused = seat_spawn.placed_elsewhere(runtime, lane, task)
    except ProfileUnresolved:
        return ""
    return f"task {task['id']} waits: {refused}" if refused else ""


def _admission(runtime: Any) -> Any:
    from scripts.swarm_v2.admission import PendingAdmission

    gate = getattr(runtime, "admission", None)
    return gate if isinstance(gate, (PendingAdmission, AdmissionCalls)) else None


def _held_back(slug: str, ledger: Any, rows: dict[str, Any], runtime: Any, task: dict[str, Any], now_ms: int) -> str:
    from scripts.swarm_v2.admission import ADMITTED, IMPOSSIBLE, REPLAYED

    gate = tick._admission(runtime)
    if gate is None:
        return ""
    try:
        decision = gate.admit(slug, [task], now_ms)[0]
    except SwarmError as exc:
        return f"task {task['id']} waits: {exc}"
    if decision.outcome == IMPOSSIBLE:
        return tick._unresolved(slug, ledger, rows, task["id"], decision.reason)
    if decision.outcome not in (ADMITTED, REPLAYED):
        return f"task {task['id']} waits: {decision.reason}"
    task["admission"] = {"reservation": decision.reservation, "deadline_ms": decision.deadline_ms}
    return ""


def _settle_admission(slug: str, runtime: Any, agent: Any) -> None:
    gate = tick._admission(runtime)
    if gate is not None and (reservation := gate.held(slug, agent.task)):
        gate.activate(slug, agent.task, reservation)


def _release_admission(slug: str, runtime: Any, task: dict[str, Any]) -> None:
    held = task.get("admission")
    if held:
        tick._admission(runtime).release(slug, task["id"], held["reservation"])


def _held_for_master(slug: str, store: Any, now_ms: int) -> list[str]:
    waiting = hash_pages.fields(store.redis, tick.MASTER_WAITING)
    others = sorted(s for s, at in waiting.items() if s != slug and now_ms - int(at) < tick.MASTER_WAIT_MS)
    return [f"holding spawns: swarm {s} waits on a session slot for its master" for s in others[:1]]


def _host_full(slug: str, store: Any, now_ms: int, in_flight: int = 0) -> str:
    capacity: Any = import_module("scripts.swarm.capacity")

    host: dict[str, Any] = capacity.read(store, slug).get("host") or {}
    if host.get("room") is None:
        return ""
    recent = tick.host_spent(store, host["granted_at"], now_ms) + in_flight
    if recent < host["room"]:
        return ""
    return f"host {host['limit']} room {host['room']}, {recent} spawned since it was granted: {host['reason']}"


def host_spent(store: Any, since_ms: int, now_ms: int) -> int:
    return store.redis.zcount(tick.HOST_SPENDS, since_ms - tick.HOST_START_LAG_MS, now_ms)


def _spend_host(store: Any, name: str, now_ms: int) -> None:
    store.redis.zadd(tick.HOST_SPENDS, {name: now_ms})
    store.redis.zremrangebyrank(tick.HOST_SPENDS, 0, -tick.HOST_SPENDS_KEPT - 1)


def _spend_local(store: Any, agent: Any, now_ms: int) -> None:
    from scripts.swarm.commands import hive_id

    if not agent.hive or agent.hive == hive_id():
        tick._spend_host(store, agent.name, now_ms)


def _release_hive_launch(slug: str, store: Any, record: Any) -> None:
    from scripts.hive import launch

    launch.release(store, slug, next((a for a in store.agents(slug) if a.name == record.name), record))


def _hold(slug: str, store: Any, text: str) -> str:
    store.redis.set(store.key(slug, tick.SPAWN_HOLD), text)
    return text


def spawn_holds(store: Any, slug: str) -> list[str]:
    held = store.redis.get(store.key(slug, tick.SPAWN_HOLD))
    return [hold for hold in (held, engine.pass_budget_hold(store, slug)) if hold]


def _spawn_stop(slug: str, config: SwarmConfig, store: Any, runtime: Any, now_ms: int, in_flight: int = 0) -> str:
    if held := engine.pass_budget_hold(store, slug):
        return held
    if stopped := run_window.claims_stopped(store, slug, config, now_ms):
        return stopped
    if not runtime.has_capacity(config):
        return "every agent is at its session cap, waiting"
    if host := tick._host_full(slug, store, now_ms, in_flight):
        return tick._hold(slug, store, f"holding spawns: {host}")
    return ""


class _LaunchClock:
    def __init__(self) -> None:
        self.begun, self.tried, self.forgiven, self.free, self.failing = tick.time.monotonic(), 0, 0, 0.0, ""
        self.preparations, self.preparing = 0, 0.0

    def spent(self, task: dict[str, Any], batch: list[Any]) -> str | None:
        if not self.preparations:
            return None
        charged = self.tried - self.forgiven
        spent = tick.time.monotonic() - self.begun - self.free if charged else self.preparing
        cost = spent / charged if charged and not batch else self.preparing / self.preparations
        if spent + cost >= tick.SPAWN_BUDGET_S:
            return f"spent this pass's {tick.SPAWN_BUDGET_S} second launch budget, {task['id']} waits a pass"
        return None

    def prepared(self, at: float) -> None:
        self.preparations, self.preparing = self.preparations + 1, self.preparing + tick.time.monotonic() - at

    def charge(self, at: float, errors: list[Exception], launches: int) -> None:
        self.tried += 1
        if len(errors) == launches and not self.forgiven:
            self.free, self.forgiven = tick.time.monotonic() - at, 1
        for error in errors:
            self.failing = tick._steered_off(error) or self.failing


def _launch_room(runtime: Any, config: SwarmConfig) -> int:
    room = getattr(runtime, "launch_room", None)
    value = room(config) if room else None
    return max(1, min(tick.SPAWN_POOL, 1 if value is None else value))


def _steer(slug: str, store: Any, task: dict[str, Any], failing: str) -> dict[str, str]:
    avoid = store.failed_harness(slug, task["id"]) or failing
    return {"avoid_harness": avoid} if avoid else {}


def _steered_off(error: Any) -> str:
    if isinstance(error, tick.SpawnError) and error.status != "unavailable":
        return error.harness
    return ""


def _record_spawn_failure(slug: str, store: Any, record: Any, error: Any, now_ms: int) -> None:
    if record_failure := timing.ON_FAILURE.get():
        record_failure(f"{tick.__name__}._spawn", error)
    waiting = isinstance(error, tick.SpawnError) and error.status == "unavailable"
    if waiting:
        transfers.detach(store, slug, record)
    else:
        transfers.failed(store, slug, record)
        store.note_launch_failure(slug, record.task, str(error), tick._steered_off(error))
    record = replace(record, pane_id=getattr(error, "pane_id", ""))
    store.record_launch(slug, record, "waiting" if waiting else "failed", str(error))
    if not waiting:
        failed_panes.hold(store, slug, record, now_ms)


def _owner_hold(slug: str, store: Any, runtime: Any, task: dict[str, Any]) -> str:
    if task["id"] not in tick._owned_tasks(slug, store):
        return ""
    tick._release_admission(slug, runtime, task)
    return f"task {task['id']} retains its live owner"


@contextmanager
def _placement_pass(runtime: Any) -> Generator[None]:
    try:
        yield
    finally:
        if end := getattr(runtime, "end_placement_pass", None):
            end()


def _past_deadline(deadline: Deadline | None, left: int, total: int) -> list[str]:
    """Name the ready tasks left for the next pass once ``deadline`` has expired, else nothing."""
    if deadline is None or not deadline.expired:
        return []
    return [f"spawn stopped at the pass deadline, {left} of {total} ready tasks left for the next pass"]


def _spawn(
    slug: str,
    config: SwarmConfig,
    store: Any,
    ledger: Any,
    runtime: Any,
    rows: dict[str, Any],
    doc: dict[str, Any],
    now_ms: int,
) -> list[str]:
    deadline = PASS_DEADLINE.get()
    agents = store.agents(slug)
    taken = {a.seat for a in agents}
    held = timing.timed(tick._held_for_master, slug, store, now_ms)
    order, actions = timing.timed(tick._spawn_order, slug, config, store, agents, rows, doc, runtime)
    if stop := _past_deadline(deadline, len(order), len(order)):
        return actions + stop
    actions += timing.timed(tick._block_unresolved, slug, ledger, rows, order)
    order = [(lane, task) for lane, task in order if profile_first.UNRESOLVED not in task]
    run = tick._Pass(slug, config, store, ledger, runtime, rows, doc, now_ms)
    clock = tick._LaunchClock()
    batch: list[Any] = []
    for index, (lane, task) in enumerate(order):
        stop = (
            held
            or _past_deadline(deadline, len(order) - index, len(order))
            or [
                timing.timed(tick._spawn_stop, slug, config, store, runtime, now_ms, len(batch))
                or clock.spent(task, batch)
            ]
        )
        if any(stop):
            return actions + tick._launch_all(run, clock, batch) + stop
        at = tick.time.monotonic()
        launch, prepared = tick._prepare(run, lane, task, taken, clock.failing, {peer.record.name for peer in batch})
        clock.prepared(at)
        actions += prepared
        batch += [launch] if launch else []
        if batch and len(batch) >= tick._launch_room(runtime, config):
            actions += tick._launch_all(run, clock, batch)
            batch = []
    return actions + tick._launch_all(run, clock, batch)


@dataclass(frozen=True)
class _Pass:
    slug: str
    config: SwarmConfig
    store: Any
    ledger: Any
    runtime: Any
    rows: dict[str, Any]
    doc: dict[str, Any]
    now_ms: int
    memo: dict[str, Any] = field(default_factory=dict[str, Any])


def _pass_reclaim(run: Any) -> partial[Any]:
    if "heads" not in run.memo:
        run.memo["heads"] = timing.call(tick.remote_heads, run.config.repo)
    return partial(tick.reclaim, heads=run.memo["heads"])


@dataclass(frozen=True)
class Launch:
    record: AgentRecord
    task: dict[str, Any]
    primed: dict[str, Any]


def _prepare(
    run: Any, lane: str, task: dict[str, Any], taken: set[str], failing: str, peers: set[str]
) -> tuple[Any, list[str]]:
    slug, config, store, ledger, runtime = run.slug, run.config, run.store, run.ledger, run.runtime
    rows, doc, now_ms = run.rows, run.doc, run.now_ms
    if ledger_client.offline():
        return None, [f"holding {task['id']}: {OFFLINE_HOLD}"]
    task = rows[task["id"]] = dict(task)
    expected = {"claimed_by": task.get("claimed_by", ""), "claim_generation": task.get("claim_generation", 0)}
    blocked = (
        timing.timed(tick._lives_spent, slug, store, ledger, rows, task)
        or timing.timed(tick._room_wait, runtime, lane, task)
        or timing.timed(tick._held_back, slug, ledger, rows, runtime, task, now_ms)
        or timing.timed(tick._owner_hold, slug, store, runtime, task)
    )
    if blocked:
        return None, [blocked]
    name = store.next_name(slug, lane, now_ms)
    if not timing.timed(store.claim, slug, task["id"], name, tick.LEASE_MS):
        tick._release_admission(slug, runtime, task)
        return None, []
    handoff = store.handoff(slug, task["id"])
    lives = store.earlier_lives(slug, task["id"])
    if handoff:
        task["handoff"] = handoff
        task["handoff_envelope"] = timing.timed(
            continuation.resolve,
            config.repo,
            task,
            rows,
            lives,
            store.handoff_envelope(slug, task["id"]) or {},
            tick._pass_reclaim(run),
        )
    elif lives or task.get("branch"):
        task["reclaim"] = timing.timed(
            continuation.resolve, config.repo, task, rows, lives, {}, tick._pass_reclaim(run)
        )
        store.put_reclaim(slug, name, task["reclaim"])
    task["stack_base"] = tick._stack_base(task, rows, config.repo)
    task["overlaps"] = timing.timed(
        tick._sharing, task, rows, timing.timed(tick._reported, slug, store, runtime, peers)
    )
    task["group"] = [
        {key: rows[m][key] for key in ("id", "title", "description")}
        for m in task.get("group_members") or []  # pyright: ignore[reportUnknownVariableType]
        if m in rows
    ]
    saved = store.redis.hget(store.key(slug, "launch-assignments"), task["id"])
    preferred = json.loads(saved)["seat"] if saved else store.handoff_seat(slug, task["id"])
    seat = tick._free_seat(slug, lane, taken, preferred)
    taken.add(seat)
    record = AgentRecord(name, lane, task["id"], started_at=now_ms, state="starting", seat=seat)
    store.put_agent(slug, record)
    try:
        state = "pr" if task.get("pr_url") else "claimed"
        fields = {
            "state": state,
            "claimed_by": name,
            "workspace": str(timing.timed(ledger_workspace.scaffold, slug, task, doc)),
        }
        if task.get("branch") in continuation.PROTECTED:
            fields["branch"] = ""
        kind = config.lanes.get(lane, {}).get("kind", "")
        if kind not in ("", "auto") and not task.get("kind"):
            fields["kind"] = kind
        live = timing.timed(ledger.update_task, slug, task["id"], fields, if_state=("open",), if_match=expected)
        task.update(live)
        if live["claimed_by"] != name:
            tick._release_admission(slug, runtime, task)
            store.release(slug, task["id"], name)
            store.drop_agent(slug, name)
            return None, [f"task {task['id']} is {live['state']} on the ledger, not claimed"]
        task.update(fields)
        timing.timed(store.record_launch, slug, record, "pending")
        store.seats.occupy(seat, name, now_ms)
        task["transfer"] = timing.timed(transfers.attach, store, slug, record)
        primed_task = {
            **successor_fill.task(timing.timed(tick.primed, store, slug, seat, task), config, lane),
            tick.ACK_LATER: True,
            "detached_launch": True,
            **tick._steer(slug, store, task, failing),
        }
    except Exception as exc:
        failed = tick.Failed(record, task, exc)
        return None, timing.timed(tick._not_launched, slug, store, ledger, runtime, rows, failed, now_ms)
    return tick.Launch(record, task, primed_task), []


def _launch(run: Any, launch: Any) -> tuple[Any, Any]:
    from scripts.swarm_v2.runtime import codex

    record = launch.record
    try:
        placed = timing.timed(run.runtime.spawn, run.config, record.lane, record.name, launch.primed)
        if placed.launch_job:
            return placed, None
        launched = timing.timed(
            codex.admit_legacy, run.store, run.slug, tick.placed_record(record, placed), run.runtime, _ports().table
        )
        return placed, launched
    except Exception as exc:
        return None, exc


def _launch_all(run: Any, clock: Any, batch: list[Any]) -> list[str]:
    launch_job: Any = import_module("scripts.swarm.launch_job")

    if not batch:
        return []
    at, contexts = tick.time.monotonic(), [copy_context() for _ in batch]
    with ThreadPoolExecutor(max_workers=len(batch)) as pool:
        outcomes: list[Any] = list(
            pool.map(lambda context, launch: context.run(tick._launch, run, launch), contexts, batch)  # pyright: ignore[reportUnknownLambdaType, reportUnknownMemberType, reportUnknownArgumentType]
        )
    clock.charge(at, [error for _, error in outcomes if isinstance(error, Exception)], len(batch))
    actions: list[str] = []
    for launch, outcome in zip(batch, outcomes):
        launch, (placed, launched), retried = tick._retry_successor(run, launch, outcome)
        actions += retried
        if placed and placed.launch_job:
            launched = launch_job.dispatch(run, launch, placed)
            if isinstance(launched, str):
                actions.append(launched)
                continue
        if isinstance(launched, Exception):
            failed = tick.Failed(launch.record, launch.task, launched)
            actions += timing.timed(
                tick._not_launched, run.slug, run.store, run.ledger, run.runtime, run.rows, failed, run.now_ms
            )
        else:
            started = tick.Started(launch.record, launch.task, placed, launched)
            actions += timing.timed(
                tick._started, run.slug, run.config, run.store, run.ledger, run.doc, started, run.now_ms
            )
    return actions


def _retry_successor(run: Any, launch: Any, outcome: tuple[Any, Any]) -> tuple[Any, tuple[Any, Any], list[str]]:
    harness = successor_retry.due(launch.task, launch.primed, outcome[1])
    if not harness:
        return launch, outcome, []
    said = successor_retry.note(run.slug, run.store, run.runtime, launch.record, outcome[1], run.now_ms)
    launch = replace(launch, primed=successor_retry.steered(launch.primed, harness))
    return launch, tick._launch(run, launch), [said]


def _reported(slug: str, store: Any, runtime: Any, peers: Collection[str] = ()) -> list[Any]:
    return [a for a in store.agents(slug) if runtime.reported(a) or a.name in peers]


def _spawn_order(
    slug: str,
    config: SwarmConfig,
    store: RedisStore,
    agents: list[AgentRecord],
    rows: dict[str, Any],
    doc: dict[str, Any],
    runtime: Any,
) -> tuple[list[Any], list[Any]]:
    capacity: Any = import_module("scripts.swarm.capacity")
    focus_budget: Any = import_module("scripts.swarm.focus_budget")

    if begin := getattr(runtime, "begin_placement_pass", None):
        timing.timed(begin)

    decision = timing.timed(capacity.read, store, slug)
    caps = timing.timed(capacity.caps, decision, config)
    dispatched = timing.timed(preempt.yielding, store, slug, doc, timing.timed(dispatcher.claim_key, config, doc))
    claims = timing.timed(tick.claim_view, slug, store, rows, doc, dispatched)
    lanes = timing.timed(tick.ready_by_lane, slug, store, rows, doc, list(caps), dispatched, claims)
    queue: list[tuple[int, str, dict[str, Any]]] = []
    waits: list[Any] = []
    for lane, cap in caps.items():
        running = timing.timed(tick._running, agents, rows, lane)
        busy = len(running)
        ready = timing.timed(readiness.launchable, config.engine, decision, lanes[lane])
        locate = partial(timing.timed, profile_first.placeable, slug, config, store, runtime, lane)
        ready, refused = timing.timed(focus_budget.place, doc, ready, lane, cap, running, locate)
        queue += [(busy + rank, lane, task) for rank, task in enumerate(ready)]
        waits += refused
    ranked = timing.timed(tick._ranked, slug, store, queue, dispatched)
    focus = timing.timed(focus_budget.ranks, doc)
    band = partial(_band, dispatched)
    return timing.timed(focus_budget.ordered, ranked, focus, band, focus_budget.outside(doc)), waits


def _band(dispatched: Callable[[dict[str, Any]], tuple[Any, ...]] | None, task: dict[str, Any]) -> Any:
    return dispatched(task)[0] if dispatched else ledger_rank.order(task)


def _ranked(
    slug: str,
    store: RedisStore,
    queue: list[tuple[int, str, dict[str, Any]]],
    dispatched: Callable[[dict[str, Any]], tuple[Any, ...]] | None,
) -> list[tuple[str, dict[str, Any]]]:
    demoted = tick._demoted(slug, store, [entry[2]["id"] for entry in queue])
    queue.sort(
        key=lambda entry: (
            _band(dispatched, entry[2]),
            entry[2]["id"] in demoted,
            entry[0],
        )
    )
    return [(lane, task) for _, lane, task in queue]


def _block_unresolved(
    slug: str, ledger: Any, rows: dict[str, Any], order: list[tuple[str, dict[str, Any]]]
) -> list[str]:
    blocked: list[str] = []
    for _, task in order:
        if deadline := PASS_DEADLINE.get():
            if deadline.expired:
                break
        if profile_first.UNRESOLVED in task:
            blocked.append(tick._unresolved(slug, ledger, rows, task["id"], task[profile_first.UNRESOLVED]))
    return blocked


def _unresolved(slug: str, ledger: Any, rows: dict[str, Any], task_id: str, reason: str) -> str:
    live = ledger.update_task(slug, task_id, {"state": "blocked"}, if_state=("open",))
    rows[task_id].update(live)
    if live["state"] != "blocked":
        return f"task {task_id} is {live['state']} on the ledger, its profile stays unresolved"
    ledger.comment(slug, task_id, reason, by="swarm")
    return f"blocked {task_id}: {reason}"


def _lives_spent(slug: str, store: Any, ledger: Any, rows: dict[str, Any], task: dict[str, Any]) -> str:
    try:
        return tick._claim_cap(slug, store, ledger, rows, task)
    except Exception as exc:
        who = Who(name="swarm", task=task["id"])
        row = gate_log.Row.of(claim_cap.GATE.name, "fail-open", who, reason=f"{type(exc).__name__}: {exc}")
        _ports().log(slug, [row])
        return ""


def _ports() -> MetricsCalls | LocalMetrics:
    """The spawn step's metrics and processes ports, else this host's gate log and process table."""
    return PASS_METRICS.get() or LocalMetrics(gate_log.append, import_module("hooks.proc").processes)


def _claim_cap(slug: str, store: Any, ledger: Any, rows: dict[str, Any], task: dict[str, Any]) -> str:
    lives, mode = store.claims(slug, task["id"]), modes.configured(claim_cap.GATE, store.config(slug).gates)
    if lives < claim_cap.CAP or mode == "off":
        return ""
    last = (store.handoff_envelope(slug, task["id"]) or {}).get("reason") or "none"  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    failure = claim_cap.launch_failure(lives, store.launches(slug), task["id"])
    reason = claim_cap.refusal(lives, last, failure, slug, task["id"])
    kind = "observe" if mode == "observe" else "deny"
    _ports().log(slug, [gate_log.Row.of(claim_cap.GATE.name, kind, Who(name="swarm", task=task["id"]), reason=reason)])
    if kind == "observe":
        return ""
    live = ledger.update_task(slug, task["id"], {"state": "blocked"}, if_state=("open",))
    rows[task["id"]].update(live)
    if live["state"] != "blocked":
        return ""
    store.reset_claims(slug, task["id"])
    ledger.comment(slug, task["id"], reason, by="swarm")
    return f"blocked {task['id']}: {reason}"


def primed(store: Any, slug: str, seat: str, task: dict[str, Any]) -> dict[str, Any]:
    priming_trace: Any = import_module("scripts.swarm.priming_trace")

    memory = {"recaps": store.memory.recaps(seat), "learned": store.memory.learned(seat)}
    envelope = task.get("handoff_envelope", store.handoff_envelope(slug, task["id"]))
    previous = store.execution_occupants(slug).get(seat)
    task = {
        **task,
        "seat": seat,
        "previous_execution_id": previous.execution_id if previous else "",
        "culture": store.culture.get(slug),
        "handoff_envelope": envelope,
        **memory,
    }
    saved = store.redis.hget(store.key(slug, "launch-assignments"), task["id"])
    if saved:
        task["launch_assignment"] = json.loads(saved)
    return priming_trace.withhold(slug, task)


def _free_seat(slug: str, lane: str, taken: set[str], preferred: str) -> str:
    if preferred and preferred not in taken:
        return preferred
    return next(
        seat for k in range(1, len(taken) + 2) if (seat := tick.seat_address(slug, seat_label(lane, k))) not in taken
    )


def placed_record(record: Any, placed: Any) -> Any:
    record = placed.execution or record
    return replace(
        record,
        pane_id=placed.pane_id,
        conversation_id=placed.conversation_id or record.conversation_id,
        harness=placed.harness,
        account=placed.account,
        model=placed.model,
        effort=placed.effort,
        placement=placed.placement,
        profile=placed.profile,
        model_source=placed.model_source,
        model_confidence=placed.model_confidence,
        profile_decision=placed.profile_decision,
        choice=placed.choice,
        launched_at=placed.launched_at or record.started_at,
        overlays=placed.overlays,
        launch_timings=placed.launch_timings,
        route_kind=placed.route_kind,
        transport=placed.transport or record.transport,
        remote_control=placed.remote_control or record.remote_control,
        desired_route_kind=record.desired_route_kind
        if placed.desired_route_kind is None
        else placed.desired_route_kind,
        state="working",
    )


STEPS = (
    CallStep("quota notice", NOTICES, held(placing(_quota_notice)), RESUMED, CONFIG_DOC),
    CallStep("lost runner", NOTICES, held(_lost_runner), RESUMED, pass_domains("config"), lane=SLOW),
    CallStep("time left", NOTICES, held(_time_left), RESUMED, CONFIG_DOC, lane=SLOW),
    CallStep(
        "codex hook order", ACKS, held(placing(_codex_hook_order)), frozenset({"sleeping"}), pass_domains("sleeping")
    ),
    WriteStep(
        "rank pass", PLACEMENT, held(placing(_rank_pass)), writes=frozenset({RANK}), needs=RESUMED, reads=CONFIG_DOC
    ),
    CallStep(
        "bottleneck remedy", PLACEMENT, held(_bottleneck_remedy), frozenset({"ledger state"}), pass_domains("doc")
    ),
    CallStep(
        "spawn",
        PLACEMENT,
        held(placing(_spawn_step)),
        budgets.SPAWN_NEEDS,
        frozenset({AGENTS, TASKS, SWARM}) | TASK_STATE,
        PLACED | pass_domains("rows"),
        lane=CallStep.lane,
        effects=frozenset({"ledger", "runtime", "coordination", "inbox", "metrics"}),
        feature="swarm-tick-spawn",
    ),
)

for _step in STEPS:
    registry.register(_step)
registry.require("scripts.tick.steps.report")

tick: Any = import_module("scripts.swarm.tick")
