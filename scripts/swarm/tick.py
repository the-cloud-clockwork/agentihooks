# pyright: reportUnusedFunction=false, reportUnusedImport=false
"""One reconcile pass over a swarm: retire finished agents, free dead or stalled agents' tasks, spawn up to the caps.

Scaling up is immediate; scaling down happens only as agents finish, so a lowered cap never kills work.
Each swarm keeps one lead master, the agent the operator talks to, which works no task; master_scale fills any
further master seats its count holds.
"""

import json
import os
import time  # noqa: F401
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any, Protocol

import scripts.tick.steps.agents  # noqa: F401
from hooks.context import context_recycle  # noqa: F401
from scripts.doctor import priming
from scripts.gates import Who  # noqa: F401
from scripts.gates import log as gate_log  # noqa: F401
from scripts.handoff import transfers
from scripts.handoff.envelope import reclaim, remote_heads  # noqa: F401
from scripts.hive import registry, reports  # noqa: F401
from scripts.inbox import exits, wake  # noqa: F401
from scripts.inbox.seats import seat_address
from scripts.inbox.store import CLOSED, InboxStore
from scripts.swarm import (
    affinity,
    apply_wait,
    bottleneck_remedy,  # noqa: F401
    claim_hold,  # noqa: F401
    claim_order,
    content_refusal,  # noqa: F401
    continuation,
    control_notifications,  # noqa: F401
    dead_resume,  # noqa: F401
    deliberate_exit,  # noqa: F401
    difficulty,  # noqa: F401
    dispatcher,  # noqa: F401
    elapsed,
    failed_panes,  # noqa: F401
    fleet_red,
    freeze,
    grouping,  # noqa: F401
    held_elsewhere,  # noqa: F401
    input_wait,  # noqa: F401
    launch_check,
    ledger_events,  # noqa: F401
    ledger_probe,  # noqa: F401
    lifetime,  # noqa: F401
    live_binding,  # noqa: F401
    lost_runner,
    master_move,
    master_questions,  # noqa: F401
    master_retire,
    master_start,
    outage,  # noqa: F401
    pane_evidence,  # noqa: F401
    panic,  # noqa: F401
    preempt,
    readiness,
    reaper,
    release_wait,
    resume,
    retire_watch,  # noqa: F401
    revoked_seats,  # noqa: F401
    run_window,  # noqa: F401
    seat_spawn,
    session_model,  # noqa: F401
    stack,
    stale_nudge,  # noqa: F401
    successor_retry,  # noqa: F401
    timing,
)
from scripts.swarm import idle as idle_state  # noqa: F401
from scripts.swarm.ledger_client import LedgerRefused, pass_state  # noqa: F401
from scripts.swarm.naming import lane_of, parse
from scripts.swarm.pane import PaneObservation
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
from scripts.swarm_ledger.repository import hierarchy
from scripts.swarm_v2 import masters as master_seats
from scripts.tick import engine, pool, steps
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain
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
from scripts.tick.steps.common import (
    _collected,  # noqa: F401
    _current_owner,
    _drop,
    _ended,
    _follow,  # noqa: F401
    _operator_line,
    _rebind,  # noqa: F401
    _reopen,  # noqa: F401
    _still_held,  # noqa: F401
    skip_dropped,  # noqa: F401
    skip_refused,  # noqa: F401
)
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
    Launch,
    Started,
    _admission,
    _block_unresolved,
    _claim_cap,
    _failed_room_wait,
    _free_seat,
    _held_back,
    _held_for_master,
    _hold,
    _host_full,
    _launch,
    _launch_all,
    _launch_room,
    _LaunchClock,
    _lives_spent,
    _not_launched,
    _owner_hold,
    _Pass,
    _pass_reclaim,
    _placement_pass,
    _ranked,
    _record_spawn_failure,
    _refused_reservation,
    _release_admission,
    _release_hive_launch,
    _reported,
    _retry_successor,
    _room_refused,
    _room_wait,
    _settle_admission,
    _spawn,
    _spawn_failed,
    _spawn_order,
    _spawn_stop,
    _spend_host,
    _spend_local,
    _started,
    _steer,
    _steered_off,
    _unresolved,
    host_spent,
    placed_record,
    primed,
    spawn_holds,
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


class SpawnError(RuntimeError):
    def __init__(self, message: str, status: str = "refused", pane_id: str = "", *, harness: str = ""):
        super().__init__(message)
        self.status = status
        self.pane_id = pane_id
        self.harness = harness

    def step_fields(self) -> dict[str, str]:
        """The message and status a failed tick step records for this launch."""
        return {"detail": str(self), "status": str(self.status)}

    def waiting(self) -> bool:
        """Whether this launch only waits: its hive acknowledgement is pending or its account reservation is full."""
        full = self.status == "unavailable" and str(self).startswith("account reservation is full")
        return self.status == "pending" or full


@dataclass(frozen=True)
class Placed:
    pane_id: str
    harness: str
    account: str = ""
    model: str = ""
    effort: str = ""
    placement: str = ""
    profile: str = ""
    model_source: str = ""
    model_confidence: float | None = None
    profile_decision: dict = field(default_factory=dict)
    choice: str = ""
    launched_at: int = 0
    overlays: list = field(default_factory=list)
    launch_timings: dict = field(default_factory=dict)
    route_kind: str = "subscription"
    transport: str = ""
    execution: AgentRecord | None = None
    remote_control: dict = field(default_factory=dict)
    desired_route_kind: str | None = None
    conversation_id: str = ""
    launch_job: dict = field(default_factory=dict)


class Ledger(Protocol):
    def state(self, slug: str) -> dict: ...
    def update_task(
        self, slug: str, task_id: str, fields: dict, by: str = ..., if_state: tuple = ..., if_match: dict | None = ...
    ) -> dict: ...
    def comment(self, slug: str, task_id: str, text: str, by: str) -> None: ...
    def notice(self, slug: str, task_id: str, text: str) -> None: ...
    def notify(self, slug: str, text: str) -> None: ...
    def closed(self, slug: str) -> bool: ...
    def bin_closed(self, slug: str, closed_at: int) -> bool: ...


class Runtime(Protocol):
    def has_capacity(self, config) -> bool: ...
    def spawn(self, config, lane: str, name: str, task: dict) -> Placed: ...
    def live_names(self) -> set[str]: ...
    def reported(self, agent: AgentRecord) -> bool: ...
    def bindings(self, agents: list[AgentRecord]) -> dict: ...
    def recover(self, name: str) -> Placed: ...
    def retire(self, agent: AgentRecord, homes: list = ...) -> bool: ...
    def refusal(self, agent: AgentRecord) -> dict: ...
    def reap_name(self, name: str) -> bool: ...
    def status(self, agent: AgentRecord) -> str: ...
    def observe(self, agent: AgentRecord) -> PaneObservation: ...
    def nudge(self, agent: AgentRecord, text: str) -> str | None: ...
    def dismiss(self, agent: AgentRecord) -> bool: ...
    def name_pane(self, agent: AgentRecord) -> bool: ...
    def conversations(self) -> dict[str, str] | None: ...
    def resume(self, config, agent: AgentRecord, text: str) -> Placed: ...
    def close_space(self, config: SwarmConfig) -> bool: ...
    def pane_tail(self, pane_id: str, lines: int) -> str: ...
    def launched_pane(self, pane_id: str, name: str) -> bool: ...
    def close_pane(self, pane_id: str) -> None: ...


class _PassView:
    """A view holding the pass state a tick group site hands its steps."""

    def __init__(self, state: dict[str, object]) -> None:
        """Keep the pass state of the group site."""
        self._state = state

    def read(self, domain: Domain, key: str) -> object | None:
        """Return the pass state under ``key``, nothing for any other domain."""
        return self._state.get(key) if domain == steps.PASS else None


def run_group(name: str, **state: Any) -> list[str]:
    """Run the registered steps of tick group ``name`` with ``state``, applying their task writes, returning actions.

    Actions of slow lane steps that end after the group returns are printed when they end.
    """
    ctx = Context(_PassView(state), Deadline.after(GROUP_BUDGET_S))
    box: Mapping[str, object] = state.get("box", {})
    printing = partial(_print_actions, box)
    records = engine.run_group(name, ctx, apply=task_steps.appliers(state), finished=printing)
    steps.note_left(box, records)
    return steps.actions(records)


def _print_actions(box: Mapping[str, object], records: tuple[engine.StepRecord, ...]) -> None:
    """Print each action the handed off steps returned, each step that failed with its error, and the exception a
    held step stored in the pass ``box``, one per line."""
    for action in steps.actions(records):
        print(action)
    for record in records:
        if record.error:
            print(f"slow lane step {record.name} failed: {record.error}")
    if "raised" in box:
        print(f"a slow lane step held {box['raised']!r}")


@reaper.scanning_pass
def tick(slug: str, store: RedisStore, ledger: Any, runtime: Any, now_ms: int) -> list[str]:
    box: dict[str, Any] = steps.PassBox()
    state: dict[str, Any] = {"slug": slug, "store": store, "ledger": ledger, "runtime": runtime, "box": box}
    sites = tick_sites.TickSites(state, box, task_steps.appliers(state), lambda: now_ms)
    gate = engine.SiteGate(sites.hooks(), sites.shut)
    records = tick_sites.run_sites(tick_sites.TICK_SITES, state, box, gate, sites.apply)
    observe_steps.raise_held(box)
    return [] if sites.left else steps.actions(records)


def _codex_hook_order():
    from scripts.targets.codex_target import codex_home, restore_hook_order

    home = codex_home()
    moved = restore_hook_order(home)
    path = (home / "hooks.json").resolve()
    return [f"restored the approved Codex hook order in {path}: {', '.join(moved)}"] if moved else []


def _close_space(slug, config, store, runtime):
    if not store.agents(slug):
        runtime.close_space(config)
    return []


def _bin_closed(slug, store, ledger, doc):
    if doc.get("closed_at") and not store.agents(slug):
        exits.close_swarm(InboxStore(store.redis), slug)
        if ledger.bin_closed(slug, doc["closed_at"]):
            return ["the closed ledger moved to the bin"]
    return []


def _woken(slug, config, store, ledger):
    inbox = InboxStore(store.redis)
    waiting = inbox.pending_items(seat_address(slug, MASTER))
    if waiting and config.template == priming.TEMPLATE and ledger.closed(slug):
        priming.cancel_master_items(inbox, slug)
        return False
    return any(_operator_line(item) for item in waiting)


def _master_down(slug, config, store, ledger, runtime, now_ms):
    """Operator lines waiting at a master seat nobody holds: tell the chat once per line, and return any line a dead
    master took but never closed to pending so the next master receives it."""
    live = runtime.live_names()
    masters = [a for a in store.agents(slug) if a.lane == MASTER and a.state != "finished"]
    if config.state == "stopping" or any(m.name in live or now_ms - m.started_at <= STARTUP_GRACE_MS for m in masters):
        return []
    inbox, seat = InboxStore(store.redis), seat_address(slug, MASTER)
    waiting = [item for item in inbox.inbox(seat) if item.state not in CLOSED and _operator_line(item)]
    for item in waiting:
        if item.state != "pending":
            inbox.redirect(item.id, "swarm", seat, REDELIVERED)
    told = [item for item in waiting if DOWN_TOLD not in (e.get("event") for e in inbox.history(item.id))]
    for item in told:
        inbox.note(item.id, DOWN_TOLD, "swarm", MASTER_DOWN, now_ms)
    if not told:
        return []
    ledger.notify(slug, MASTER_DOWN)
    return [f"master down, told the operator about {len(told)} waiting lines"]


def agent_status(agent):
    if agent.state in {"finished", "awaiting-decision"}:
        return agent.state
    if agent.input_prompt:
        return "waiting"
    if agent.idle_ticks >= IDLE_NUDGE_TICKS:
        return "stalled"
    return "idle" if agent.idle_ticks else "working"


def ready_in_order(slug, store, rows, doc, lane, dispatched=None, claims=None):
    return _launch_order(slug, store, _claimable(slug, store, rows, doc, lane, dispatched, claims), dispatched)


def _owned_tasks(slug, store):
    return {agent.task for agent in store.agents(slug) if agent.state != "finished"}


def _claimable(slug, store, rows, doc, lane, dispatched=None, claims=None):
    claims = claims or claim_view(slug, store, rows, doc, dispatched)
    held = list(claims.held)
    clear, overlapping = [], []
    for t in claims.ordered:
        if readiness.claimable(t, lane, claims.view):
            mine = t.get("territory") or []
            if any(_overlaps(mine, other) for other in held):
                overlapping.append(t)
                continue
            clear.append(t)
            held.append(mine)
    return clear + overlapping


@dataclass(frozen=True)
class Claims:
    view: readiness.Snapshot
    held: list
    ordered: list


def claim_view(
    slug: str,
    store: RedisStore,
    rows: dict[str, dict[str, Any]],
    doc: dict[str, Any],
    dispatched: Callable[[dict[str, Any]], tuple[Any, ...]] | None = None,
) -> Claims:
    awaiting = _owned_tasks(slug, store)
    claimed = store.claimed(slug, [task_id for task_id, t in rows.items() if t.get("state") == "open"])
    held = [t.get("territory") or [] for t in rows.values() if t.get("state") in ACTIVE]
    config = store.config(slug)
    graph, fix = hierarchy.project(doc)[0], freeze.fix_phase(config)
    released, applied = release_wait.checker(config), apply_wait.checker(config, rows)
    unshared = fleet_red.checker(fleet_red.saved_reader(store, slug))
    view = readiness.Snapshot(
        slug,
        rows,
        doc,
        graph,
        fix,
        lambda task_id: task_id in awaiting or task_id in claimed,
        lambda task: released(task) and applied(task) and unshared(task),
    )
    return Claims(view, held, sorted(rows.values(), key=dispatched or claim_order.key(rows)))


def _launch_order(slug, store, tasks, dispatched=None):
    demoted = _demoted(slug, store, [task["id"] for task in tasks])
    return sorted(
        tasks,
        key=lambda task: (
            dispatched(task)[0] if dispatched else ledger_rank.order(task),
            task["id"] in demoted,
            not claim_order.resumed(task),
        ),
    )


def _demoted(slug, store, task_ids):
    failed = store.launch_failures(slug)
    held = [task_id for task_id in task_ids if failed.get(task_id)]
    return set(held) - store.handoff_tasks(slug, held) if held else set()


def _parked(task, rows):
    return any(rows.get(dep, {}).get("state") != "done" for dep in task.get("parked_on") or [])


def _stack_base(task, rows, repo, origin=None):
    deps = [dep for dep in task.get("depends_on") or [] if rows[dep].get("state") != "done"]
    return [_stacked_dep(dep, rows[dep], repo, origin or stack.origin_key) for dep in deps]


def _stacked_dep(dep, row, repo, origin):
    base = {"task": dep, "branch": row["branch"]}
    elsewhere = row.get("branch_repo") and stack.repo_key(row["branch_repo"]) != origin(repo)
    return {**base, "repo": row["branch_repo"]} if elsewhere else base


def _refund_parked(slug, store, rows, agent):
    reason = (store.handoff_envelope(slug, agent.task) or {}).get("reason")
    preempted = preempt.take_victim(store, slug, agent.name)
    if _parked(rows.get(agent.task, {}), rows) or reason == "quota" or (reason == "operator" and preempted):
        store.refund_held_claim(slug, agent.task, agent.name, store.claim_generation(slug, agent.task))


def _master_set(doc, task_id):
    by = doc.get("_meta", {}).get("stamps", {}).get(f"tasks/{task_id}/state", {}).get("by")
    return "master" if lane_of(by) == MASTER else ""


def _overlaps(mine, theirs):
    return bool(_shared(mine, theirs))


def _shared(mine, theirs):
    pairs = [(a, b) for a in map(_area, mine) for b in map(_area, theirs)]
    return sorted({b if _nested(a, b) else a for a, b in pairs if _nested(a, b) or _nested(b, a)})


def _sharing(task, rows, agents):
    mine = task.get("territory") or []
    owners = {a.task: a.name for a in agents if not _ended(a, rows)}
    running = [t for t in rows.values() if t.get("state") in ACTIVE and t["id"] != task["id"]]
    return [
        {
            "task": t["id"],
            "claimant": owners.get(t["id"], ""),
            "areas": areas,
            **({"checks": t["state"] == "pr"} if t["id"] not in owners else {}),
        }
        for t in running
        if (areas := _shared(mine, t.get("territory") or []))
    ]


def _area(entry):
    return entry.strip().removeprefix("./").rstrip("/")


def _nested(outer, inner):
    return inner == outer or inner.startswith(outer + "/")


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


def _recover_master(slug, config, store, runtime, now_ms):
    recorded = [a for a in store.agents(slug) if a.lane == MASTER]
    agents = [a for a in recorded if master_seats.is_lead(slug, a)]
    live = runtime.live_names()
    if any(a.state != "finished" and a.name in live for a in agents):
        return []
    finished = {a.name for a in recorded}
    seat = seat_address(slug, MASTER)
    occupant = store.seats.occupant(seat).occupant
    candidates = []
    for name in sorted(live - finished):
        parsed = parse(name)
        if name == occupant or (parsed and parsed.kind == MASTER and parsed.code == config.code):
            candidates.append(name)
        elif name.startswith(f"{slug}-master-"):
            candidates.append(name)
    if not candidates:
        return []
    name = occupant if occupant in candidates else candidates[0]
    record = store.execution_occupants(slug).get(seat)
    if not record or record.name != name:
        record = AgentRecord(name, MASTER, MASTER, started_at=now_ms, seat=seat)
    store.put_agent(slug, placed_record(record, runtime.recover(name)))
    for agent in agents:
        if agent.name not in live and agent.state != "finished":
            store.drop_agent(slug, agent.name)
    if occupant != name:
        store.seats.occupy(seat, name, now_ms)
    return [f"adopted live master {name}"]


def _night_watch(slug, store, ledger, runtime, doc, now_ms, sleeping):
    from scripts.swarm.health import night_watch

    def respawn():
        if master_start.read(store, slug).get("alerted"):
            store.redis.delete(store.key(slug, "master-start"))
        with PLACING:
            return _master(slug, store.config(slug), store, runtime, now_ms, past_host=True)

    return night_watch.run(night_watch.Watch(store, slug, ledger, runtime, doc, now_ms, sleeping, respawn))


def _master(slug, config, store, runtime, now_ms, past_host=False):
    store.redis.hdel(MASTER_WAITING, slug)
    agents = store.agents(slug)
    masters = [a for a in agents if a.lane == MASTER]
    if config.state == "stopping":
        if any(a.lane != MASTER for a in agents):
            return []
        return [_retire_master(slug, store, runtime, m, now_ms) for m in masters]
    masters = [m for m in masters if master_seats.is_lead(slug, m)]
    pending = master_start.read(store, slug)
    if any(m.state != "finished" for m in masters) or master_start.blocked(pending, now_ms):
        return []
    if any(m.name in runtime.live_names() for m in masters):
        return ["the old master is still running, waiting for it to end before starting the next"]
    if refused := seat_spawn.placed_elsewhere(runtime, MASTER, {}):
        return [refused]
    if refused := seat_spawn.no_slot(config, runtime, MASTER):
        store.redis.hset(MASTER_WAITING, slug, now_ms)
        return [refused]
    if not past_host and (held := seat_spawn.host_hold(slug, store, now_ms, MASTER)):
        return [held]

    launched = {}

    def prepare(record):
        transfer = transfers.attach(store, slug, record)
        task = (
            {**pending["task"], "transfer": transfer}
            if pending
            else primed(
                store,
                slug,
                record.seat,
                {"id": MASTER, "handoff": store.handoff(slug, MASTER), "peer": store.peer(slug), "transfer": transfer},
            )
        )
        from scripts.swarm import master_launch

        task = master_launch._filled(master_move.pinned(store, slug, task, transfer), config)
        master_start.begin(store, slug, record.name, task, now_ms)
        affinity.handed_off(store, slug)
        launched.update(task)
        return task

    try:
        record, placed = seat_spawn.place(slug, config, store, runtime, MASTER, now_ms, prepare)
    except seat_spawn.SeatFailed as spawn_failure:
        exc, record = spawn_failure.error, spawn_failure.record
        store.drop_agent(slug, record.name)
        if seat_spawn.room_refused(exc):
            transfers.detach(store, slug, record)
            master_start.withdraw(store, slug, pending)
            return [f"the master waits: {exc}"]
        transfers.failed(store, slug, record)
        affinity.failed(store, slug, str(exc))
        return master_start.failed(store, slug, f"master spawn failed: {exc}", now_ms)
    affinity.placed(store, slug, placed.harness)
    master_move.placed(store, slug)
    record = placed_record(record, placed)
    reported = runtime.reported(record)
    store.put_agent(slug, replace(record, state="working" if reported else "starting"))
    launch_check.begin(store, slug, record, now_ms)
    if reported:
        store.redis.delete(store.key(slug, "master-start"))
        store.clear_handoff(slug, MASTER)
    store.redis.hdel(store.key(slug, "launch-assignments"), MASTER)
    return [f"spawned master {record.name}", *master_move.account_moved(slug, launched, record)]


def _retire_master(slug, store, runtime, master, now_ms):
    live = master.name in runtime.live_names()
    if held := master_retire.hold(store, slug, master, "the swarm is stopping", live, now_ms):
        return held
    if not runtime.retire(master, homes=reaper.scratch_homes(slug, master.task)):
        return f"could not retire {master.name}, retrying next tick"
    store.drop_agent(slug, master.name)
    return f"retired {master.name}"


def respawn_master(slug, store, runtime, now_ms):
    from scripts.swarm import controller

    def respawn(config, held):
        fenced = controller.FencedRuntime(store, slug, held, runtime, controller.LOCAL)
        live, actions = fenced.live_names(), []
        for agent in store.agents(slug):
            gone = agent.state not in ("finished", "starting") and agent.name not in live
            if agent.lane == MASTER and gone and master_seats.is_lead(slug, agent):
                store.drop_agent(slug, agent.name)
                actions.append(f"dropped the gone master {agent.name}")
        return actions + (
            _recover_master(slug, config, store, fenced, now_ms) or _master(slug, config, store, fenced, now_ms)
        )

    return _under_tick_lock(slug, store, respawn, "respawn master")


def place_master(slug, store, runtime, now_ms):
    from scripts.swarm import controller

    def place(config, held):
        fenced = controller.FencedRuntime(store, slug, held, runtime, controller.deployment_for(config, os.environ))
        return _master(slug, config, store, fenced, now_ms)

    return _under_tick_lock(slug, store, place, "place master")


def _under_tick_lock(slug: str, store: RedisStore, act: Any, caller: str) -> Any:
    from scripts.swarm import controller, lease
    from scripts.swarm.commands import hive_id

    if store.config(slug).state in ("stopping", "stopped"):
        return []
    held = lease.acquire(store, slug, hive_id())
    if held is None:
        return []
    token = controller.take_tick_lock(store, slug, held, lease.ttl_ms(), caller)
    if token is None:
        return []
    try:
        config = store.ensure_code(slug)
        if config.state in ("stopping", "stopped"):
            return []
        with lease.held_pass(store, slug, held):
            return act(config, held)
    finally:
        controller.release_tick_lock(store, slug, token, caller)


def _settle(
    slug: str, config: SwarmConfig, store: RedisStore, ledger: Any, rows: dict[str, dict[str, Any]], doc: Any
) -> list[str]:
    agents = store.agents(slug)
    if config.state == "stopping":
        if not any(a.lane == MASTER and a.state != "finished" for a in agents):
            _end_awaiting_seats(slug, store, ledger, rows, agents)
            agents = [a for a in agents if a.state != resume.AWAITING]
        if agents:
            return []
        store.update(slug, state="stopped")
        return ["stopped"]
    if any(a.lane != MASTER for a in agents):
        return []
    if config.state != "running" or any(_claimable(slug, store, rows, doc, lane) for lane in LANES):
        return []
    store.update(slug, state="drained")
    blocked = sum(1 for t in rows.values() if t.get("state") == "blocked" and not t.get("out_of_scope"))
    waiting = {0: "", 1: ", one blocked task waits for you"}.get(blocked, f", {blocked} blocked tasks wait for you")
    ledger.notify(slug, freeze.notice(doc, rows.values(), freeze.fix_phase(config)) + waiting)
    return ["drained"]


def _end_awaiting_seats(
    slug: str, store: RedisStore, ledger: Any, rows: dict[str, dict[str, Any]], agents: list[AgentRecord]
) -> None:
    for agent in agents:
        if agent.state == resume.AWAITING:
            _drop(slug, store, ledger, rows, agent)
