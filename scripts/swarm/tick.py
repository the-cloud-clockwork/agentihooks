# pyright: reportUnusedFunction=false, reportUnusedImport=false, reportPrivateUsage=false
"""One reconcile pass over a swarm: retire finished agents, free dead or stalled agents' tasks, spawn up to the caps.

Scaling up is immediate; scaling down happens only as agents finish, so a lowered cap never kills work.
Each swarm keeps one lead master, the agent the operator talks to, which works no task; master_scale fills any
further master seats its count holds.
"""

import json
import os  # noqa: F401
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any

import scripts.tick.steps.agents  # noqa: F401
from hooks.context import context_recycle  # noqa: F401
from scripts.doctor import priming  # noqa: F401
from scripts.gates import Who, modes
from scripts.gates import claims as claim_cap
from scripts.gates import log as gate_log
from scripts.handoff import transfers
from scripts.handoff.envelope import reclaim, remote_heads
from scripts.hive import registry, reports  # noqa: F401
from scripts.inbox import exits, wake  # noqa: F401
from scripts.inbox.seats import seat_address
from scripts.inbox.store import CLOSED, InboxStore  # noqa: F401
from scripts.swarm import (
    affinity,  # noqa: F401
    apply_wait,  # noqa: F401
    bottleneck_remedy,  # noqa: F401
    claim_hold,  # noqa: F401
    claim_order,  # noqa: F401
    content_refusal,  # noqa: F401
    continuation,
    control_notifications,  # noqa: F401
    dead_resume,  # noqa: F401
    deliberate_exit,  # noqa: F401
    difficulty,  # noqa: F401
    dispatcher,
    elapsed,
    failed_panes,
    fleet_red,  # noqa: F401
    freeze,  # noqa: F401
    grouping,  # noqa: F401
    held_elsewhere,  # noqa: F401
    input_wait,  # noqa: F401
    launch_check,  # noqa: F401
    ledger_events,  # noqa: F401
    ledger_probe,  # noqa: F401
    lifetime,  # noqa: F401
    live_binding,  # noqa: F401
    lost_runner,
    master_move,  # noqa: F401
    master_questions,  # noqa: F401
    master_retire,  # noqa: F401
    master_start,  # noqa: F401
    outage,  # noqa: F401
    pane_evidence,  # noqa: F401
    panic,  # noqa: F401
    preempt,
    profile_first,
    readiness,
    reaper,
    release_wait,  # noqa: F401
    resume,  # noqa: F401
    retire_watch,  # noqa: F401
    revoked_seats,  # noqa: F401
    run_window,
    seat_spawn,  # noqa: F401
    session_model,  # noqa: F401
    stack,  # noqa: F401
    stale_nudge,  # noqa: F401
    successor_retry,
    timing,
)
from scripts.swarm import idle as idle_state  # noqa: F401
from scripts.swarm.ledger_client import LedgerRefused, pass_state  # noqa: F401
from scripts.swarm.naming import lane_of, parse, seat_label  # noqa: F401
from scripts.swarm.profile_choice import ProfileUnresolved  # noqa: F401
from scripts.swarm.ready import awaiting as awaiting_merge  # noqa: F401
from scripts.swarm.store import (  # noqa: F401
    EVENTS_ENGINE,
    MASTER,
    PREFIX,
    AgentRecord,
    RedisStore,
    SwarmConfig,
    SwarmError,
)
from scripts.swarm_ledger import ledger_rank, ledger_workspace
from scripts.swarm_ledger.repository import hierarchy  # noqa: F401
from scripts.swarm_v2 import masters as master_seats  # noqa: F401
from scripts.tick import engine, pool, steps
from scripts.tick.deadline import Deadline  # noqa: F401
from scripts.tick.step import Context, Domain  # noqa: F401
from scripts.tick.steps import observe as observe_steps
from scripts.tick.steps import tasks as task_steps
from scripts.tick.steps.agents import (  # noqa: F401
    _drop_remote,
    _elsewhere,
    _free_refused,
    _free_revoked,
    _free_stuck,
    _hive_lost,
    _live_names,
    _lost,
    _observe,
    _operator_at_pane,
    _reap_agent,
    _Reaping,
    _refusal_due,
    _remote_gone_reason,
    _restore_owner,
    _retire_ended,
    _stale_nudge,
    _stuck_on_provider,
    _watch_agent,
    _watch_idle,
    _watch_remote,
)

from scripts.tick import sites as tick_sites  # isort: skip
from scripts.tick.steps.claims import (  # noqa: F401
    Claims,
    Ledger,
    Placed,
    Runtime,
    SpawnError,
    _area,
    _bin_closed,
    _claimable,
    _close_space,
    _demoted,
    _end_awaiting_seats,
    _launch_order,
    _master_set,
    _nested,
    _overlaps,
    _owned_tasks,
    _parked,
    _refund_parked,
    _settle,
    _shared,
    _sharing,
    _stack_base,
    _stacked_dep,
    agent_status,
    claim_view,
    ready_in_order,
)
from scripts.tick.steps.common import (
    _collected,  # noqa: F401
    _current_owner,
    _drop,  # noqa: F401
    _ended,
    _follow,  # noqa: F401
    _operator_line,  # noqa: F401
    _rebind,  # noqa: F401
    _reopen,  # noqa: F401
    _still_held,  # noqa: F401
    skip_dropped,  # noqa: F401
    skip_refused,  # noqa: F401
)
from scripts.tick.steps.masters import (  # noqa: F401
    _codex_hook_order,
    _master,
    _master_down,
    _night_watch,
    _PassView,
    _print_actions,
    _recover_master,
    _retire_master,
    _under_tick_lock,
    _woken,
    place_master,
    respawn_master,
)
from scripts.tick.steps.masters import _run_group as run_group  # noqa: F401
from scripts.tick.steps.observe import (  # noqa: F401
    _acknowledging,
    _collect_acks,
    _expire_launch,
    _failed_hive_launch,
    _failed_launch,
    _finished_launch,
    _launch_checks,
    _retire_launch,
    _verify,
)
from scripts.tick.steps.place import (  # noqa: F401
    Failed,
    Started,
    _admission,
    _failed_room_wait,
    _held_back,
    _not_launched,
    _refused_reservation,
    _release_admission,
    _room_refused,
    _room_wait,
    _settle_admission,
    _spawn_failed,
    _started,
)

LEASE_MS = 10 * 60 * 1000
SPAWN_BUDGET_S = 60
SPAWN_POOL = 8
STARTUP_GRACE_MS = 6 * 60 * 1000
ACK_LATER = "hive_ack_later"
ACKNOWLEDGING = "acknowledging"
ACK_EXPIRED = "no hive acknowledged the launch within the claim lease"
EXITED_BEFORE_LIFE = "its agent process exited before its launch check passed"
SUSPECT = "suspect"
MASTER_WAITING = f"{PREFIX}:master-waiting"
MASTER_WAIT_MS = 10 * 60 * 1000
SPAWN_HOLD = "spawn-hold"
GROUP_BUDGET_S = 600.0
MASTER_MOVE_MODULES = ("scripts.hive.launch", "scripts.hive.daemon", "scripts.swarm.local_slots")
SPAWN_DUE = "spawn-due"
HOST_SPENDS = f"{PREFIX}:host-spends"
HOST_SPENDS_KEPT = 1000
HOST_START_LAG_MS = 30 * 1000
PLACING = pool.HostLock(lambda: lost_runner.home() / "swarm" / "placing.lock")
DOWN_TOLD = "master down told"
REDELIVERED = "the master went down before closing it; kept for the next master"
MASTER_DOWN = (
    "The master is down and is being relaunched. Your message waits for the new master, which receives it as soon "
    "as it starts."
)
IDLE_NUDGE_MS = 2 * elapsed.PASS_MS
IDLE_KILL_MS = 19 * elapsed.PASS_MS
IDLE_NUDGE_TICKS = elapsed.passes(IDLE_NUDGE_MS)
IDLE_KILL_TICKS = elapsed.passes(IDLE_KILL_MS)
IDLE_TICKS = "idle-ticks"
LANES = ("eng", "ci", "plan")
ACTIVE = ("claimed", "pr")
NUDGE = (
    "Swarm check: you are idle and your task is still open. If you are waiting on checks or a deploy, declare it "
    "with agentihooks swarm {slug} wait <minutes> --reason <what> and keep waiting. Otherwise finish it with agentihooks swarm {slug} done "
    "and the proof your task's kind needs (--pr <url> for code), or agentihooks swarm {slug} block with the reason. "
    "Answer with those commands or agentihooks msg reply, never as text in this terminal."
)


@reaper.scanning_pass
def tick(slug: str, store: RedisStore, ledger: Any, runtime: Any, now_ms: int) -> list[str]:
    box: dict[str, Any] = steps.PassBox()
    state: dict[str, Any] = {"slug": slug, "store": store, "ledger": ledger, "runtime": runtime, "box": box}
    sites = tick_sites.TickSites(state, box, task_steps.appliers(state), lambda: now_ms)
    gate = engine.SiteGate(sites.hooks(), sites.shut)
    records = tick_sites.run_sites(tick_sites.TICK_SITES, state, box, gate, sites.apply)
    observe_steps.raise_held(box)
    return [] if sites.left else steps.actions(records)


def _held_for_master(slug, store, now_ms):
    waiting = store.redis.hgetall(MASTER_WAITING)
    others = sorted(s for s, at in waiting.items() if s != slug and now_ms - int(at) < MASTER_WAIT_MS)
    return [f"holding spawns: swarm {s} waits on a session slot for its master" for s in others[:1]]


def _host_full(slug, store, now_ms, in_flight=0):
    from scripts.swarm import capacity

    host = capacity.read(store, slug).get("host") or {}
    if host.get("room") is None:
        return ""
    recent = host_spent(store, host["granted_at"], now_ms) + in_flight
    if recent < host["room"]:
        return ""
    return f"host {host['limit']} room {host['room']}, {recent} spawned since it was granted: {host['reason']}"


def host_spent(store, since_ms: int, now_ms: int) -> int:
    return store.redis.zcount(HOST_SPENDS, since_ms - HOST_START_LAG_MS, now_ms)


def _spend_host(store, name, now_ms):
    store.redis.zadd(HOST_SPENDS, {name: now_ms})
    store.redis.zremrangebyrank(HOST_SPENDS, 0, -HOST_SPENDS_KEPT - 1)


def _spend_local(store, agent, now_ms):
    from scripts.swarm.commands import hive_id

    if not agent.hive or agent.hive == hive_id():
        _spend_host(store, agent.name, now_ms)


def _release_hive_launch(slug, store, record):
    from scripts.hive import launch

    launch.release(store, slug, next((a for a in store.agents(slug) if a.name == record.name), record))


def _hold(slug, store, text):
    store.redis.set(store.key(slug, SPAWN_HOLD), text)
    return text


def spawn_holds(store: Any, slug: str) -> list[str]:
    held = store.redis.get(store.key(slug, SPAWN_HOLD))
    return [hold for hold in (held, engine.pass_budget_hold(store, slug)) if hold]


def _spawn_stop(slug: str, config: SwarmConfig, store: Any, runtime: Any, now_ms: int, in_flight: int = 0) -> str:
    if held := engine.pass_budget_hold(store, slug):
        return held
    if stopped := run_window.claims_stopped(store, slug, config, now_ms):
        return stopped
    if not runtime.has_capacity(config):
        return "every agent is at its session cap, waiting"
    if host := _host_full(slug, store, now_ms, in_flight):
        return _hold(slug, store, f"holding spawns: {host}")
    return ""


class _LaunchClock:
    def __init__(self) -> None:
        self.begun, self.tried, self.forgiven, self.free, self.failing = time.monotonic(), 0, 0, 0.0, ""
        self.preparations, self.preparing = 0, 0.0

    def spent(self, task: dict, batch: list) -> str | None:
        if not self.preparations:
            return None
        charged = self.tried - self.forgiven
        spent = time.monotonic() - self.begun - self.free if charged else self.preparing
        cost = spent / charged if charged and not batch else self.preparing / self.preparations
        if spent + cost >= SPAWN_BUDGET_S:
            return f"spent this pass's {SPAWN_BUDGET_S} second launch budget, {task['id']} waits a pass"
        return None

    def prepared(self, at: float) -> None:
        self.preparations, self.preparing = self.preparations + 1, self.preparing + time.monotonic() - at

    def charge(self, at, errors, launches):
        self.tried += 1
        if len(errors) == launches and not self.forgiven:
            self.free, self.forgiven = time.monotonic() - at, 1
        for error in errors:
            self.failing = _steered_off(error) or self.failing


def _launch_room(runtime, config):
    room = getattr(runtime, "launch_room", None)
    return max(1, min(SPAWN_POOL, room(config) if room else 1))


def _steer(slug, store, task, failing):
    avoid = store.failed_harness(slug, task["id"]) or failing
    return {"avoid_harness": avoid} if avoid else {}


def _steered_off(error):
    if isinstance(error, SpawnError) and error.status != "unavailable":
        return error.harness
    return ""


def _record_spawn_failure(slug, store, record, error, now_ms):
    if record_failure := timing.ON_FAILURE.get():
        record_failure(f"{__name__}._spawn", error)
    waiting = isinstance(error, SpawnError) and error.status == "unavailable"
    if waiting:
        transfers.detach(store, slug, record)
    else:
        transfers.failed(store, slug, record)
        store.note_launch_failure(slug, record.task, str(error), _steered_off(error))
    record = replace(record, pane_id=getattr(error, "pane_id", ""))
    store.record_launch(slug, record, "waiting" if waiting else "failed", str(error))
    if not waiting:
        failed_panes.hold(store, slug, record, now_ms)


def _owner_hold(slug, store, runtime, task):
    if task["id"] not in _owned_tasks(slug, store):
        return ""
    _release_admission(slug, runtime, task)
    return f"task {task['id']} retains its live owner"


@contextmanager
def _placement_pass(runtime):
    try:
        yield
    finally:
        if end := getattr(runtime, "end_placement_pass", None):
            end()


def _spawn(
    slug: str,
    config: SwarmConfig,
    store: object,
    ledger: object,
    runtime: object,
    rows: dict,
    doc: dict,
    now_ms: int,
) -> list:
    agents = store.agents(slug)
    taken = {a.seat for a in agents}
    held = timing.timed(_held_for_master, slug, store, now_ms)
    order, actions = timing.timed(_spawn_order, slug, config, store, agents, rows, doc, runtime)
    actions += timing.timed(_block_unresolved, slug, ledger, rows, order)
    order = [(lane, task) for lane, task in order if profile_first.UNRESOLVED not in task]
    run = _Pass(slug, config, store, ledger, runtime, rows, doc, now_ms)
    clock, batch = _LaunchClock(), []
    for lane, task in order:
        stop = held or [
            timing.timed(_spawn_stop, slug, config, store, runtime, now_ms, len(batch)) or clock.spent(task, batch)
        ]
        if any(stop):
            return actions + _launch_all(run, clock, batch) + stop
        at = time.monotonic()
        launch, prepared = _prepare(run, lane, task, taken, clock.failing, {peer.record.name for peer in batch})
        clock.prepared(at)
        actions += prepared
        batch += [launch] if launch else []
        if batch and len(batch) >= _launch_room(runtime, config):
            actions += _launch_all(run, clock, batch)
            batch = []
    return actions + _launch_all(run, clock, batch)


@dataclass(frozen=True)
class _Pass:
    slug: str
    config: SwarmConfig
    store: object
    ledger: object
    runtime: object
    rows: dict
    doc: dict
    now_ms: int
    memo: dict = field(default_factory=dict)


def _pass_reclaim(run):
    if "heads" not in run.memo:
        run.memo["heads"] = timing.call(remote_heads, run.config.repo)
    return partial(reclaim, heads=run.memo["heads"])


@dataclass(frozen=True)
class Launch:
    record: AgentRecord
    task: dict
    primed: dict


def _prepare(run, lane, task, taken, failing, peers):
    slug, config, store, ledger, runtime = run.slug, run.config, run.store, run.ledger, run.runtime
    rows, doc, now_ms = run.rows, run.doc, run.now_ms
    task = rows[task["id"]] = dict(task)
    expected = {"claimed_by": task.get("claimed_by", ""), "claim_generation": task.get("claim_generation", 0)}
    blocked = (
        timing.timed(_lives_spent, slug, store, ledger, rows, task)
        or timing.timed(_room_wait, runtime, lane, task)
        or timing.timed(_held_back, slug, ledger, rows, runtime, task, now_ms)
        or timing.timed(_owner_hold, slug, store, runtime, task)
    )
    if blocked:
        return None, [blocked]
    name = store.next_name(slug, lane, now_ms)
    if not timing.timed(store.claim, slug, task["id"], name, LEASE_MS):
        _release_admission(slug, runtime, task)
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
            _pass_reclaim(run),
        )
    elif lives or task.get("branch"):
        task["reclaim"] = timing.timed(continuation.resolve, config.repo, task, rows, lives, {}, _pass_reclaim(run))
        store.put_reclaim(slug, name, task["reclaim"])
    task["stack_base"] = _stack_base(task, rows, config.repo)
    task["overlaps"] = timing.timed(_sharing, task, rows, timing.timed(_reported, slug, store, runtime, peers))
    task["group"] = [
        {key: rows[m][key] for key in ("id", "title", "description")}
        for m in task.get("group_members") or []
        if m in rows
    ]
    saved = store.redis.hget(store.key(slug, "launch-assignments"), task["id"])
    preferred = json.loads(saved)["seat"] if saved else store.handoff_seat(slug, task["id"])
    seat = _free_seat(slug, lane, taken, preferred)
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
        kind = config.lanes.get(lane, {}).get("kind", "")
        if kind not in ("", "auto") and not task.get("kind"):
            fields["kind"] = kind
        live = timing.timed(ledger.update_task, slug, task["id"], fields, if_state=("open",), if_match=expected)
        task.update(live)
        if live["claimed_by"] != name:
            _release_admission(slug, runtime, task)
            store.release(slug, task["id"], name)
            store.drop_agent(slug, name)
            return None, [f"task {task['id']} is {live['state']} on the ledger, not claimed"]
        task.update(fields)
        timing.timed(store.record_launch, slug, record, "pending")
        store.seats.occupy(seat, name, now_ms)
        task["transfer"] = timing.timed(transfers.attach, store, slug, record)
        primed_task = {
            **timing.timed(primed, store, slug, seat, task),
            ACK_LATER: True,
            "detached_launch": True,
            **_steer(slug, store, task, failing),
        }
    except Exception as exc:
        failed = Failed(record, task, exc)
        return None, timing.timed(_not_launched, slug, store, ledger, runtime, rows, failed, now_ms)
    return Launch(record, task, primed_task), []


def _launch(run, launch):
    from scripts.swarm_v2.runtime import codex

    record = launch.record
    try:
        placed = timing.timed(run.runtime.spawn, run.config, record.lane, record.name, launch.primed)
        if placed.launch_job:
            return placed, None
        launched = timing.timed(codex.admit_legacy, run.store, run.slug, placed_record(record, placed), run.runtime)
        return placed, launched
    except Exception as exc:
        return None, exc


def _launch_all(run, clock, batch):
    from scripts.swarm import launch_job

    if not batch:
        return []
    at, contexts = time.monotonic(), [copy_context() for _ in batch]
    with ThreadPoolExecutor(max_workers=len(batch)) as pool:
        outcomes = list(pool.map(lambda context, launch: context.run(_launch, run, launch), contexts, batch))
    clock.charge(at, [error for _, error in outcomes if isinstance(error, Exception)], len(batch))
    actions = []
    for launch, outcome in zip(batch, outcomes):
        launch, (placed, launched), retried = _retry_successor(run, launch, outcome)
        actions += retried
        if placed and placed.launch_job:
            launched = launch_job.dispatch(run, launch, placed)
            if isinstance(launched, str):
                actions.append(launched)
                continue
        if isinstance(launched, Exception):
            failed = Failed(launch.record, launch.task, launched)
            actions += timing.timed(
                _not_launched, run.slug, run.store, run.ledger, run.runtime, run.rows, failed, run.now_ms
            )
        else:
            started = Started(launch.record, launch.task, placed, launched)
            actions += timing.timed(_started, run.slug, run.config, run.store, run.ledger, run.doc, started, run.now_ms)
    return actions


def _retry_successor(run, launch, outcome):
    harness = successor_retry.due(launch.task, launch.primed, outcome[1])
    if not harness:
        return launch, outcome, []
    said = successor_retry.note(run.slug, run.store, run.runtime, launch.record, outcome[1], run.now_ms)
    launch = replace(launch, primed=successor_retry.steered(launch.primed, harness))
    return launch, _launch(run, launch), [said]


def _reported(slug, store, runtime, peers=()):
    return [a for a in store.agents(slug) if runtime.reported(a) or a.name in peers]


def _acknowledging_error(exc):
    from scripts.swarm_v2.runtime.base import Status

    return isinstance(exc, SpawnError) and exc.status == Status.PENDING


def _running(agents: list[AgentRecord], rows: dict[str, Any], lane: str) -> list[dict[str, Any]]:
    """The task rows of the lane's agents that have not ended and still own their task."""
    return [
        rows.get(a.task, {"id": a.task, "lane": a.lane})
        for a in agents
        if a.lane == lane and not _ended(a, rows) and _current_owner(a, rows.get(a.task, {}))
    ]


def _spawn_order(
    slug: str,
    config: SwarmConfig,
    store: RedisStore,
    agents: list[AgentRecord],
    rows: dict[str, Any],
    doc: dict[str, Any],
    runtime: Any,
) -> tuple[list[Any], list[Any]]:
    from scripts.swarm import capacity, focus_budget

    if begin := getattr(runtime, "begin_placement_pass", None):
        timing.timed(begin)

    decision = timing.timed(capacity.read, store, slug)
    caps = timing.timed(capacity.caps, decision, config)
    dispatched = timing.timed(preempt.yielding, store, slug, doc, timing.timed(dispatcher.claim_key, config, doc))
    claims = timing.timed(claim_view, slug, store, rows, doc, dispatched)
    queue, waits = [], []
    for lane, cap in caps.items():
        running = timing.timed(_running, agents, rows, lane)
        busy = len(running)
        ready = timing.timed(ready_in_order, slug, store, rows, doc, lane, dispatched, claims)
        ready = timing.timed(readiness.launchable, config.engine, decision, ready)
        locate = partial(timing.timed, profile_first.placeable, slug, config, store, runtime, lane)
        ready, refused = timing.timed(focus_budget.place, doc, ready, lane, cap, running, locate)
        queue += [(busy + rank, lane, task) for rank, task in enumerate(ready)]
        waits += refused
    ranked = timing.timed(_ranked, slug, store, queue, dispatched)
    focus = timing.timed(focus_budget.ranks, doc)
    return timing.timed(focus_budget.ordered, ranked, focus), waits


def _ranked(
    slug: str,
    store: RedisStore,
    queue: list[tuple[int, str, dict[str, Any]]],
    dispatched: Callable[[dict[str, Any]], tuple[Any, ...]] | None,
) -> list[tuple[str, dict[str, Any]]]:
    demoted = _demoted(slug, store, [entry[2]["id"] for entry in queue])
    queue.sort(
        key=lambda entry: (
            dispatched(entry[2])[0] if dispatched else ledger_rank.order(entry[2]),
            entry[2]["id"] in demoted,
            entry[0],
        )
    )
    return [(lane, task) for _, lane, task in queue]


def _block_unresolved(slug, ledger, rows, order):
    blocked = []
    for _, task in order:
        if profile_first.UNRESOLVED in task:
            blocked.append(_unresolved(slug, ledger, rows, task["id"], task[profile_first.UNRESOLVED]))
    return blocked


def _unresolved(slug, ledger, rows, task_id, reason):
    live = ledger.update_task(slug, task_id, {"state": "blocked"}, if_state=("open",))
    rows[task_id].update(live)
    if live["state"] != "blocked":
        return f"task {task_id} is {live['state']} on the ledger, its profile stays unresolved"
    ledger.comment(slug, task_id, reason, by="swarm")
    return f"blocked {task_id}: {reason}"


def _lives_spent(slug, store, ledger, rows, task):
    try:
        return _claim_cap(slug, store, ledger, rows, task)
    except Exception as exc:  # a crashed gate lets the claim through, counted in the gate log
        who = Who(name="swarm", task=task["id"])
        gate_log.append(
            slug, gate_log.Row.of(claim_cap.GATE.name, "fail-open", who, reason=f"{type(exc).__name__}: {exc}")
        )
        return ""


def _claim_cap(slug, store, ledger, rows, task):
    lives, mode = store.claims(slug, task["id"]), modes.configured(claim_cap.GATE, store.config(slug).gates)
    if lives < claim_cap.CAP or mode == "off":
        return ""
    last = (store.handoff_envelope(slug, task["id"]) or {}).get("reason") or "none"
    failure = claim_cap.launch_failure(lives, store.launches(slug), task["id"])
    reason = claim_cap.refusal(lives, last, failure, slug, task["id"])
    kind = "observe" if mode == "observe" else "deny"
    gate_log.append(slug, gate_log.Row.of(claim_cap.GATE.name, kind, Who(name="swarm", task=task["id"]), reason=reason))
    if kind == "observe":
        return ""
    live = ledger.update_task(slug, task["id"], {"state": "blocked"}, if_state=("open",))
    rows[task["id"]].update(live)
    if live["state"] != "blocked":
        return ""
    store.reset_claims(slug, task["id"])
    ledger.comment(slug, task["id"], reason, by="swarm")
    return f"blocked {task['id']}: {reason}"


def primed(store, slug, seat, task):
    from scripts.swarm import priming_trace

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


def _free_seat(slug, lane, taken, preferred):
    if preferred and preferred not in taken:
        return preferred
    return next(
        seat for k in range(1, len(taken) + 2) if (seat := seat_address(slug, seat_label(lane, k))) not in taken
    )


def placed_record(record, placed):
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
