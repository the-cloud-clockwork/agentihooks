# pyright: reportUnusedFunction=false, reportUnusedImport=false, reportPrivateUsage=false
"""One reconcile pass over a swarm: retire finished agents, free dead or stalled agents' tasks, spawn up to the caps.

Scaling up is immediate; scaling down happens only as agents finish, so a lowered cap never kills work.
Each swarm keeps one lead master, the agent the operator talks to, which works no task; master_scale fills any
further master seats its count holds.
"""

import json
import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import dataclass, field, replace
from functools import partial
from importlib import import_module
from typing import Any

import scripts.tick.steps.agents  # noqa: F401
from hooks.context import context_recycle  # noqa: F401
from scripts.doctor import priming  # noqa: F401
from scripts.gates import Who, modes
from scripts.gates import claims as claim_cap
from scripts.gates import log as gate_log
from scripts.handoff import transfers
from scripts.handoff.envelope import reclaim, remote_heads
from scripts.hive import registry, reports
from scripts.inbox import exits, wake
from scripts.inbox.seats import seat_address
from scripts.inbox.store import CLOSED, InboxStore  # noqa: F401
from scripts.swarm import (
    affinity,  # noqa: F401
    apply_wait,  # noqa: F401
    bottleneck_remedy,  # noqa: F401
    claim_hold,  # noqa: F401
    claim_order,  # noqa: F401
    content_refusal,
    continuation,
    control_notifications,  # noqa: F401
    dead_resume,
    deliberate_exit,
    difficulty,  # noqa: F401
    dispatcher,
    elapsed,
    failed_panes,
    fleet_red,  # noqa: F401
    freeze,  # noqa: F401
    grouping,  # noqa: F401
    held_elsewhere,
    input_wait,
    launch_check,  # noqa: F401
    ledger_events,  # noqa: F401
    ledger_probe,
    lifetime,  # noqa: F401
    live_binding,  # noqa: F401
    lost_runner,
    master_move,  # noqa: F401
    master_questions,
    master_retire,
    master_start,  # noqa: F401
    outage,
    pane_evidence,  # noqa: F401
    panic,  # noqa: F401
    preempt,
    profile_first,
    readiness,
    reaper,
    release_wait,  # noqa: F401
    resume,
    retire_watch,
    revoked_seats,
    run_window,
    seat_spawn,  # noqa: F401
    session_model,  # noqa: F401
    stack,  # noqa: F401
    stale_nudge,
    successor_retry,
    timing,
)
from scripts.swarm import idle as idle_state
from scripts.swarm.ledger_client import LedgerRefused, pass_state  # noqa: F401
from scripts.swarm.naming import lane_of, parse, seat_label  # noqa: F401
from scripts.swarm.profile_choice import ProfileUnresolved  # noqa: F401
from scripts.swarm.ready import awaiting as awaiting_merge
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
from scripts.tick.steps import masters as master_steps
from scripts.tick.steps import observe as observe_steps
from scripts.tick.steps import place
from scripts.tick.steps import tasks as task_steps
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
    _drop,
    _ended,
    _follow,  # noqa: F401
    _operator_line,  # noqa: F401
    _rebind,  # noqa: F401
    _reopen,
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
from scripts.tick.steps.masters import _run_group as run_group
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
    for name in MASTER_MOVE_MODULES:
        import_module(name)
    box: dict[str, Any] = steps.PassBox()
    passing = {"slug": slug, "store": store, "ledger": ledger, "runtime": runtime, "now_ms": now_ms, "box": box}
    actions = run_group("start", **passing)
    observe_steps.raise_held(box)
    if "finished" in box:
        return actions
    actions += run_group(observe_steps.PASS_SETUP, **passing)
    actions += run_group("observe", **passing)
    observe_steps.raise_held(box)
    config, doc, rows, due = box["config"], box["doc"], box["rows"], store.key(slug, SPAWN_DUE)
    passing |= {"rows": rows, "doc": doc, "due": due}
    actions += run_group("agents", **passing, config=config)
    actions.extend(master_steps.run_masters(locals()))
    actions += run_group(task_steps.DRAINED, **passing, config=config)
    task_steps.raise_held(box)
    config, sleeping = box["config"], task_steps.sleeping_of(box)
    actions += run_group("tasks", **passing, config=config)
    actions += run_group(task_steps.REOPENS, **passing, config=config)
    actions += run_group(task_steps.TASK_PASSES, **passing, config=config)
    task_steps.raise_held(box)
    with PLACING:
        run_group("place")
        actions += run_group(place.NOTICES, **passing, config=config)
        actions += run_group(place.ACKS, **passing, config=config, sleeping=sleeping)
        task_steps.raise_held(box)
        if not sleeping:
            actions.extend(master_steps.run_seats(locals()))
            actions += run_group(place.PLACEMENT, **passing, config=config)
            task_steps.raise_held(box)
        actions.extend(master_steps.run_dispatch(locals()))
    actions.extend(master_steps.run_watch(locals()))
    passing["rows"] = box["rows"]
    actions += run_group(task_steps.CLOSE, **passing, config=config)
    task_steps.raise_held(box)
    return actions


def _restore_owner(
    slug: str, store: RedisStore, ledger: Any, rows: dict[str, dict[str, Any]], agent: AgentRecord
) -> None:
    row = rows.get(agent.task, {})
    if (
        _ended(agent, rows)
        or not _current_owner(agent, row)
        or not timing.timed(store.ensure_claim, slug, agent, LEASE_MS)
    ):
        return
    if row.get("out_of_scope") or row.get("state") not in ("open", "claimed", "pr"):
        return
    owner = row.get("claimed_by", "")
    if owner == agent.name and row["state"] != "open":
        return
    expected = {
        "claimed_by": owner,
        "claim_generation": row.get("claim_generation", 0),
        "awaiting": row.get("awaiting", ""),
    }
    fields = {"claimed_by": agent.name}
    if row["state"] == "open":
        fields["state"] = "pr" if row.get("pr_url") else "claimed"
    rows[agent.task] = timing.timed(
        ledger.update_task, slug, agent.task, fields, if_state=(row["state"],), if_match=expected
    )


def _drop_remote(slug, store, ledger, rows, agent, reason):
    master_retire.forget(store, slug, agent.name)
    _refund_parked(slug, store, rows, agent)
    return f"retired {agent.name}: {reason}" + _drop(slug, store, ledger, rows, agent)


def _remote_gone_reason(slug: str, store: RedisStore, runtime: Any, agent: AgentRecord, now_ms: int) -> str:
    hives = reports.Reports(store.redis)
    if _elsewhere(agent) and hives.vanished(agent, now_ms):
        return "its hive no longer lists it"
    if not reports.host(agent):
        return ""
    if hives.gone(agent) or runtime.observe(agent).state == "exited":
        return "its hive pane is gone"
    return "its hive stopped reporting" if _hive_lost(slug, store, agent, now_ms) else ""


def _hive_lost(slug, store, agent, now_ms):
    if agent.state == "finished":
        return reports.Reports(store.redis).lost(agent, now_ms, registry.LIVE_MS)
    if not reports.Reports(store.redis).lost(agent, now_ms, reports.LOST_MS):
        return False
    beat = idle_state.heartbeat(store.redis, slug, agent.name)
    wait = idle_state.wait(store.redis, slug, agent.name)
    fresh = beat is not None and now_ms - beat["at"] <= reports.LOST_MS
    return not fresh and (wait is None or wait["until"] <= now_ms)


def _retire_ended(slug, store, ledger, runtime, rows, agent, now_ms):
    master_retire.forget(store, slug, agent.name)
    removed = reports.Reports(store.redis).removed(agent, now_ms)
    if not removed and not runtime.retire(agent, homes=reaper.scratch_homes(slug, agent.task)):
        retire_watch.failed(store, slug, agent.name, runtime.refusal(agent), now_ms)
        return [f"could not retire {agent.name}, retrying next tick"]
    _refund_parked(slug, store, rows, agent)
    store.release(slug, agent.task, agent.name)
    store.drop_agent(slug, agent.name)
    row = rows.get(agent.task, {})
    goes_on = bool(store.handoff(slug, agent.task)) and row.get("state") in ACTIVE and not awaiting_merge(row)
    if goes_on:
        _reopen(slug, ledger, rows, agent.task)
    exits.settle(InboxStore(store.redis), agent.name, agent.seat if goes_on else "", "exited")
    return [f"retired {agent.name}"]


def _live_names(runtime):
    return runtime.live_names()


def _observe(runtime, agent):
    return runtime.observe(agent)


def _elsewhere(agent: AgentRecord) -> bool:
    """A hub admitted agent names its hive before that hive records a host target."""
    from scripts.swarm.commands import hive_id

    return not agent.runtime_target and bool(agent.hive) and agent.hive != hive_id()


@dataclass(frozen=True)
class _Reaping:
    """What one reap pass judges every agent against."""

    slug: str
    store: RedisStore
    ledger: Any
    runtime: Any
    rows: dict[str, dict[str, Any]]
    live: Any
    now_ms: int


def _reap_agent(judging: _Reaping, agent: AgentRecord) -> list[str]:
    """Return the actions judging one agent: drop, retire, watch, refresh its lease or count it lost."""
    slug, store, ledger, runtime = judging.slug, judging.store, judging.ledger, judging.runtime
    rows, live, now_ms = judging.rows, judging.live, judging.now_ms
    if (
        agent.state == "awaiting-decision"
        or (agent.lane == MASTER and agent.state == "starting")
        or timing.timed(_acknowledging, slug, store, agent)
    ):
        return []
    if reason := timing.timed(_remote_gone_reason, slug, store, runtime, agent, now_ms):
        if not _ended(agent, rows) and held_elsewhere.placed(slug, store, agent, now_ms):
            store.refresh(slug, agent.task, agent.name, LEASE_MS)
            return []
        return [timing.timed(_drop_remote, slug, store, ledger, rows, agent, reason)]
    ended = _ended(agent, rows)
    if agent.state == "retiring" and not ended:
        return []
    if ended:
        return timing.timed(_retire_ended, slug, store, ledger, runtime, rows, agent, now_ms)
    if agent.runtime_backend != "local" or reports.host(agent) or _elsewhere(agent):
        return timing.timed(_watch_remote, slug, store, ledger, runtime, rows, agent, now_ms)
    if agent.name in live and agent.lane == MASTER:
        runtime.name_pane(agent)
        return timing.timed(_watch_agent, slug, store, ledger, runtime, rows, agent, now_ms)
    if agent.name in live:
        timing.timed(_restore_owner, slug, store, ledger, rows, agent)
        if _ended(agent, rows):
            return timing.timed(_retire_ended, slug, store, ledger, runtime, rows, agent, now_ms)
        return timing.timed(_watch_agent, slug, store, ledger, runtime, rows, agent, now_ms)
    if resume.recovery_pending(store, slug, agent):
        store.refresh(slug, agent.task, agent.name, LEASE_MS)
        return []
    if now_ms - agent.started_at > STARTUP_GRACE_MS:
        return timing.timed(_lost, slug, store, ledger, runtime, rows, agent, now_ms)
    return []


def _lost(slug, store, ledger, runtime, rows, agent, now_ms):
    if deliberate_exit.take(store, slug, agent.name):
        runtime.retire(agent, homes=reaper.scratch_homes(slug, agent.task))
        return [
            f"retired {agent.name} after a deliberate terminate" + _drop(slug, store, ledger, rows, agent, refund=True)
        ]
    if dead_resume.reopen(slug, store, ledger, runtime, agent, now_ms):
        return [f"resumed {agent.name}"]
    if agent not in store.agents(slug):
        return []
    runtime.retire(agent, homes=reaper.scratch_homes(slug, agent.task))
    return [f"lost {agent.name}" + _drop(slug, store, ledger, rows, agent)]


def _watch_remote(
    slug: str,
    store: RedisStore,
    ledger: Any,
    runtime: Any,
    rows: dict[str, dict[str, Any]],
    agent: AgentRecord,
    now_ms: int,
) -> list[str]:
    """A remote agent is judged by its own runtime, never by this host's process table: no answer keeps it suspect,
    holding its claim, neither retired nor dropped."""
    timing.timed(store.refresh, slug, agent.task, agent.name, LEASE_MS)
    if runtime.observe(agent).state == "unknown":
        if agent.state == SUSPECT:
            return []
        timing.timed(store.put_agent, slug, replace(agent, state=SUSPECT))
        return [f"suspect {agent.name}: its runtime did not answer"]
    if agent.state == SUSPECT:
        agent = replace(agent, state="working")
        timing.timed(store.put_agent, slug, agent)
    return timing.timed(_watch_agent, slug, store, ledger, runtime, rows, agent, now_ms)


def _refusal_due(slug, store, rows, agent, observed, now_ms):
    if not observed.content_refusal:
        return False
    row = rows.get(agent.task)
    if not row or row.get("claimed_by") != agent.name or _operator_at_pane(slug, store, agent, observed, now_ms):
        return False
    return content_refusal.repeated(store, slug, agent, observed)


def _free_refused(slug, store, ledger, runtime, rows, agent, now_ms):
    if not runtime.retire(agent, homes=reaper.scratch_homes(slug, agent.task)):
        return [f"could not retire {agent.name} after repeated content refusals, retrying next tick"]
    content_refusal.handoff(store, slug, agent, now_ms)
    return [
        f"freed {agent.name} after repeated content refusals" + _drop(slug, store, ledger, rows, agent, refund=True)
    ]


def _free_revoked(slug, store, ledger, runtime, rows, agent, now_ms):
    account = f"{agent.harness} account {agent.account}"
    if agent.lane == MASTER:
        revoked_seats.recycle_master(slug, store, ledger, agent, now_ms)
        return [f"recycled master {agent.name} in its seat: {account} was revoked"]
    if not runtime.retire(agent, homes=reaper.scratch_homes(slug, agent.task)):
        return [f"could not retire {agent.name} on revoked {account}, retrying next tick"]
    revoked_seats.worker_handoff(store, slug, agent, now_ms)
    return [
        f"freed the seat of {agent.name}: {account} was revoked" + _drop(slug, store, ledger, rows, agent, refund=True)
    ]


def _watch_agent(
    slug: str,
    store: RedisStore,
    ledger: Any,
    runtime: Any,
    rows: dict[str, dict[str, Any]],
    agent: AgentRecord,
    now_ms: int,
) -> list[str]:
    if timing.timed(revoked_seats.on_revoked, agent):
        return timing.timed(_free_revoked, slug, store, ledger, runtime, rows, agent, now_ms)
    return _watch_idle(slug, store, ledger, runtime, rows, agent, now_ms)


def _watch_idle(
    slug: str,
    store: RedisStore,
    ledger: Any,
    runtime: Any,
    rows: dict[str, dict[str, Any]],
    agent: AgentRecord,
    now_ms: int,
) -> list[str]:
    from scripts.swarm import automated_gc

    observed = timing.timed(_observe, runtime, agent)
    if timing.timed(_refusal_due, slug, store, rows, agent, observed, now_ms):
        return timing.timed(_free_refused, slug, store, ledger, runtime, rows, agent, now_ms)
    timing.timed(store.redis.set, store.key(slug, "pane-state", agent.name), observed.state, ex=idle_state.BEAT_TTL_S)
    timing.timed(outage.mark_pane, store, slug, agent.name, observed.provider_error, now_ms)
    if observed.provider_error and not observed.prompt_title and not observed.typed:
        observed = replace(observed, state=idle_state.IDLE)
    if observed.state == idle_state.WAITING:
        title = observed.prompt_title or "Waiting on input"
        same = agent.input_prompt == title
        began = timing.timed(input_wait.mark, store, slug, agent.name, now_ms, agent.input_ticks if same else 0)
        ticks = elapsed.passes(now_ms - began)
        waiting = replace(agent, input_prompt=title, input_ticks=ticks, idle_ticks=0)
        timing.timed(store.put_agent, slug, waiting)
        return (
            timing.timed(master_questions.dismiss, slug, store, ledger, runtime, waiting, now_ms)
            if agent.lane == MASTER
            else []
        )
    if agent.input_prompt:
        timing.timed(input_wait.clear, store, slug, agent.name)
        if agent.lane == MASTER:
            timing.timed(master_questions.drop, store, slug, agent.name)
    agent = replace(agent, input_prompt="", input_ticks=0)
    held = timing.timed(outage.held, store, slug, agent.name)
    exhausted = timing.timed(resume.recovery_exhausted, store, slug, agent)
    pending = timing.timed(resume.recovery_pending, store, slug, agent)
    if timing.timed(_stuck_on_provider, slug, store, agent, observed, exhausted, now_ms):
        return timing.timed(_free_stuck, slug, store, ledger, runtime, rows, agent, now_ms)
    claimant = rows.get(agent.task, {}).get("claimed_by") == agent.name
    nudged = _stale_nudge(slug, store, agent, observed, pending, now_ms) if claimant else []
    if agent.lane == MASTER or (held and not exhausted):
        timing.timed(elapsed.hold, store, slug, elapsed.IDLE_SINCE, agent.name, agent.idle_ticks)
        timing.timed(store.put_agent, slug, agent)
        return nudged
    state = idle_state.verdict(
        observed.state,
        timing.timed(idle_state.heartbeat, store.redis, slug, agent.name),
        timing.timed(idle_state.wait, store.redis, slug, agent.name),
        now_ms,
    )
    if state in {idle_state.WAITING, idle_state.WORKING}:
        timing.timed(store.put_agent, slug, replace(agent, idle_ticks=0))
        return nudged
    if (
        timing.timed(_operator_at_pane, slug, store, agent, observed, now_ms)
        or timing.timed(ledger_probe.holding, store, slug)
        or timing.timed(automated_gc.owned, slug, agent)
    ):
        timing.timed(elapsed.hold, store, slug, elapsed.IDLE_SINCE, agent.name, agent.idle_ticks)
        timing.timed(store.put_agent, slug, agent)
        return nudged
    since = timing.timed(elapsed.start, store, slug, elapsed.IDLE_SINCE, agent.name, now_ms, agent.idle_ticks)
    idle = replace(agent, idle_ticks=elapsed.passes(now_ms - since))
    timing.timed(store.put_agent, slug, idle)
    who = Who(name=agent.name, task=agent.task)
    timing.timed(
        gate_log.append,
        slug,
        gate_log.Row.of(IDLE_TICKS, "count", who, reason=f"idle tick {idle.idle_ticks}", now_ms=now_ms),
    )
    if timing.timed(resume.recovery_holds, store, slug, agent, now_ms):
        return nudged
    if idle.idle_ticks >= IDLE_KILL_TICKS and runtime.retire(idle, homes=reaper.scratch_homes(slug, idle.task)):
        why = (
            f"; outage recovery could not proceed: {timing.timed(resume.recovery_blocker, store, slug, agent)}"
            if pending
            else ""
        )
        dropped = timing.timed(_drop, slug, store, ledger, rows, idle, refund=True)
        return nudged + [f"stalled {agent.name}" + dropped + why]
    return nudged


def _stale_nudge(
    slug: str, store: RedisStore, agent: AgentRecord, observed: Any, pending: bool, now_ms: int
) -> list[str]:
    """An open swarm outage and a pending recovery belong to provider recovery; an agent due for retirement gets none."""
    if agent.lane == MASTER or not agent.task or pending or timing.timed(outage.open_records, slug):
        return []
    if timing.timed(elapsed.due, store, slug, elapsed.IDLE_SINCE, agent.name, now_ms, agent.idle_ticks, IDLE_KILL_MS):
        return []
    if timing.timed(_operator_at_pane, slug, store, agent, observed, now_ms) or timing.timed(
        ledger_probe.holding, store, slug
    ):
        return []
    return stale_nudge.nudge(InboxStore(store.redis), slug, agent, observed.provider_error, now_ms)


def _stuck_on_provider(slug, store, agent, observed, exhausted, now_ms):
    if agent.lane == MASTER or not observed.provider_error or _operator_at_pane(slug, store, agent, observed, now_ms):
        return False
    if exhausted:
        return True
    state = idle_state.verdict(
        observed.state,
        idle_state.heartbeat(store.redis, slug, agent.name),
        idle_state.wait(store.redis, slug, agent.name),
        now_ms,
    )
    return state == idle_state.IDLE and outage.held_past_limit(store, slug, agent.name, now_ms)


def _free_stuck(slug, store, ledger, runtime, rows, agent, now_ms):
    if not runtime.retire(agent, homes=reaper.scratch_homes(slug, agent.task)):
        store.put_agent(slug, agent)
        return [f"could not retire {agent.name}, stuck on a provider error, retrying next tick"]
    resume.free_seat(store, slug, agent, now_ms)
    outage.clear_pane(store, slug, agent.name)
    return [
        f"freed the seat of {agent.name}, stuck on a provider error"
        + _drop(slug, store, ledger, rows, agent, refund=True)
    ]


def _operator_at_pane(slug, store, agent, observed, now_ms):
    """The idle count holds while the pane's input line holds text or the operator prompted it inside the quiet window."""
    last = idle_state.last_prompt(store.redis, slug, agent.name)
    return bool(observed.typed) or (last is not None and now_ms - last < wake.quiet_ms(os.environ))


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
