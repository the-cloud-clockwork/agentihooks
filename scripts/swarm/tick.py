# pyright: reportUnusedFunction=false, reportUnusedImport=false, reportPrivateUsage=false
"""One reconcile pass over a swarm: retire finished agents, free dead or stalled agents' tasks, spawn up to the caps.

Scaling up is immediate; scaling down happens only as agents finish, so a lowered cap never kills work.
Each swarm keeps one lead master, the agent the operator talks to, which works no task; master_scale fills any
further master seats its count holds.
"""

import os  # noqa: F401
import time  # noqa: F401
from dataclasses import dataclass, field, replace  # noqa: F401
from typing import Any

import scripts.tick.steps.agents  # noqa: F401
from hooks.context import context_recycle  # noqa: F401
from scripts.doctor import priming  # noqa: F401
from scripts.gates import Who  # noqa: F401
from scripts.gates import log as gate_log  # noqa: F401
from scripts.handoff import transfers  # noqa: F401
from scripts.handoff.envelope import reclaim, remote_heads  # noqa: F401
from scripts.hive import registry, reports  # noqa: F401
from scripts.inbox import exits, wake  # noqa: F401
from scripts.inbox.seats import seat_address  # noqa: F401
from scripts.inbox.store import CLOSED, InboxStore  # noqa: F401
from scripts.swarm import (
    affinity,  # noqa: F401
    apply_wait,  # noqa: F401
    bottleneck_remedy,  # noqa: F401
    claim_hold,  # noqa: F401
    claim_order,  # noqa: F401
    content_refusal,  # noqa: F401
    control_notifications,  # noqa: F401
    dead_resume,  # noqa: F401
    deliberate_exit,  # noqa: F401
    difficulty,  # noqa: F401
    dispatcher,  # noqa: F401
    elapsed,
    failed_panes,  # noqa: F401
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
    preempt,  # noqa: F401
    readiness,  # noqa: F401
    reaper,
    release_wait,  # noqa: F401
    resume,  # noqa: F401
    retire_watch,  # noqa: F401
    revoked_seats,  # noqa: F401
    run_window,  # noqa: F401
    seat_spawn,  # noqa: F401
    session_model,  # noqa: F401
    stack,  # noqa: F401
    stale_nudge,  # noqa: F401
    successor_retry,  # noqa: F401
    timing,  # noqa: F401
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
from scripts.swarm_ledger import ledger_rank, ledger_workspace  # noqa: F401
from scripts.swarm_ledger.repository import hierarchy  # noqa: F401
from scripts.swarm_v2 import masters as master_seats  # noqa: F401
from scripts.tick import engine, pool, steps
from scripts.tick.deadline import Deadline  # noqa: F401
from scripts.tick.step import Context  # noqa: F401
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

from scripts.tick import sites as tick_sites  # isort: skip
from scripts.tick.steps.claims import (  # noqa: F401
    Claims,
    Ledger,
    Placed,
    Runtime,
    SpawnError,
    _claimable,
    _demoted,
    _launch_order,
    _owned_tasks,
    agent_status,
    claim_view,
    ready_by_lane,
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
    _retire_or_stop,  # noqa: F401
    _still_held,  # noqa: F401
    _stop_hive,  # noqa: F401
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
    _prepare,
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
from scripts.tick.steps.territory import (  # noqa: F401
    _area,
    _bin_closed,
    _close_space,
    _end_awaiting_seats,
    _master_set,
    _nested,
    _overlaps,
    _parked,
    _refund_parked,
    _settle,
    _shared,
    _sharing,
    _stack_base,
    _stacked_dep,
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
    from scripts.swarm.cli import _pass_tools
    from scripts.tick.ports.tools import PASS_TOOLS

    token = PASS_TOOLS.set(_pass_tools(store, slug, "tick", ledger, runtime))
    try:
        box: dict[str, Any] = steps.PassBox()
        state: dict[str, Any] = {"slug": slug, "store": store, "ledger": ledger, "runtime": runtime, "box": box}
        sites = tick_sites.TickSites(state, box, task_steps.appliers(state), lambda: now_ms)
        gate = engine.SiteGate(sites.hooks(), sites.shut, sites=tick_sites.TICK_SITES)
        records = tick_sites.run_sites(
            tick_sites.TICK_SITES, state, box, gate, sites.apply, tick_sites.TICK_HOOK_KEYS, tick_sites.TICK_SHUT_KEYS
        )
        observe_steps.raise_held(box)
        return [] if sites.left else steps.actions(records)
    finally:
        PASS_TOOLS.reset(token)


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
