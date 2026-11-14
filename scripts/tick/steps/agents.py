"""The agent steps of the tick pass: reap, outages, strays, failed panes, orphans and session upkeep."""

# pyright: reportUnusedFunction=false

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from typing import Any, cast

from scripts.swarm.store import AgentRecord, RedisStore
from scripts.tick import registry
from scripts.tick.adapters.calls import LedgerCalls, RuntimeCalls
from scripts.tick.deadline import Deadline
from scripts.tick.engine import SLOW
from scripts.tick.steps import AGENTS, DEADLINE, TASKS, CallStep, pass_domains

Get = Callable[[str], Any]
_IDENTITY_CONFLICT = "stale or changed execution identity"


def _reap(
    slug: str,
    store: Any,
    ledger: Any,
    runtime: Any,
    rows: dict[str, dict[str, Any]],
    now_ms: int,
    deadline: Deadline | None = None,
) -> list[str]:
    """Judge every agent of the swarm, legacy and hub admitted, one task per pass worker, returning actions in agent
    order.

    Past ``deadline`` no further agent is judged: the rest are left for the next pass, named in one closing action.
    """
    from scripts.hub import owned_leases
    from scripts.hub.reconciler import admitted_agents, legacy_agents

    live, rows = tick.timing.timed(tick._live_names, runtime), dict(rows)

    judging = tick._Reaping(slug, store, ledger, runtime, rows, live, now_ms)
    agents = list(tick.timing.timed(legacy_agents, store, slug))
    tick.timing.timed(owned_leases.renew, store, slug, agents, tick.LEASE_MS)
    admitted = list(tick.timing.timed(admitted_agents, store, slug))
    hub_owned = {agent.name for agent in admitted}
    agents += admitted
    by_task: dict[str, list[int]] = {}
    for index, agent in enumerate(agents):
        by_task.setdefault(agent.task or agent.name, []).append(index)

    def one(indexes: list[int]) -> list[tuple[int, list[str]]]:
        """Return the actions judging each agent of one task took, in agent order, until the deadline passes."""
        judged: list[tuple[int, list[str]]] = []
        for index in indexes:
            if deadline is not None and deadline.expired:
                break
            agent = agents[index]
            with tick.timing.acting(agent.name):
                try:
                    judge = _reap_admitted if agent.name in hub_owned else tick._reap_agent
                    judged.append((index, _kept_on_refusal(agent.name, judge(judging, agent))))
                except tick.SwarmError as exc:
                    if _IDENTITY_CONFLICT not in str(exc):
                        raise
                    print(f"reap skipped {agent.name}, its execution identity changed mid pass: {exc}", file=sys.stderr)
                    judged.append((index, [f"skipped {agent.name}, its execution identity changed mid pass"]))
        return judged

    with tick.ledger_probe.read_once():
        found = dict(pair for pairs in tick.pool.fan_out(one, by_task.values()) for pair in pairs)
    actions = [action for index in sorted(found) for action in found[index]]
    left = len(agents) - len(found)
    if left:
        actions.append(f"reap stopped at the pass deadline, {left} of {len(agents)} agents left for the next pass")
    return actions


def _kept_on_refusal(name: str, actions: list[str]) -> list[str]:
    """Report an agent whose drop the store refused as kept for the next pass, never as retired or lost."""
    from scripts.tick.steps.common import DROP_REFUSED

    return [f"kept {name}{DROP_REFUSED}" if action.endswith(DROP_REFUSED) else action for action in actions]


def _strays(slug: Any, config: Any, store: Any, runtime: Any) -> Any:
    """Live agent processes still carrying a name this swarm issued and retired: every holder is left over."""
    actions: list[str]
    recorded, actions = {a.name for a in store.agents(slug)}, []
    for name in sorted(runtime.live_names() - recorded):
        found = tick.parse(name)
        if not found or found.code != config.code or not store.names.entry(name).get("retired_at"):
            continue
        if runtime.reap_name(name):
            actions.append(f"reaped stray {name}")
        else:
            actions.append(f"could not reap stray {name}, retrying next tick")
    return actions


def _orphans(slug: Any, store: Any, ledger: Any, rows: Any) -> Any:
    known = {a.name for a in store.agents(slug)}
    actions: list[str] = []
    for task_id, row in rows.items():
        if (
            row.get("state") in tick.ACTIVE
            and row.get("claimed_by") not in known
            and store.claimant(slug, task_id) is None
            and not tick.awaiting_merge(row)
        ):
            if _reopen_orphan(slug, ledger, rows, task_id):
                actions.append(f"task {task_id} had no agent, reopened")
    return actions


def _reopen_orphan(slug: Any, ledger: Any, rows: Any, task_id: str) -> bool:
    """Reopen an orphaned row only while it still holds the claimant and claim generation the pass read."""
    row = rows[task_id]
    expected = {"claimed_by": row.get("claimed_by", ""), "claim_generation": row.get("claim_generation", 0)}
    return tick._reopen(slug, ledger, rows, task_id, expected)


def _restore_owner(slug: Any, store: Any, ledger: Any, rows: Any, agent: Any) -> None:
    row = rows.get(agent.task, {})
    if (
        tick._ended(agent, rows)
        or not tick._current_owner(agent, row)
        or not tick.timing.timed(store.ensure_claim, slug, agent, tick.LEASE_MS)
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
    rows[agent.task] = tick.timing.timed(
        ledger.update_task, slug, agent.task, fields, if_state=(row["state"],), if_match=expected
    )


def _drop_remote(slug: Any, store: Any, ledger: Any, rows: Any, agent: Any, reason: Any) -> str:
    tick.master_retire.forget(store, slug, agent.name)
    tick._refund_parked(slug, store, rows, agent)
    return f"retired {agent.name}: {reason}" + tick._drop(slug, store, ledger, rows, agent)


def _remote_gone_reason(slug: str, store: Any, runtime: Any, agent: AgentRecord, now_ms: int) -> str:
    hives = tick.reports.Reports(store.redis)
    if tick._elsewhere(agent) and hives.vanished(agent, now_ms):
        return "its hive no longer lists it"
    if _unbound_past_window(slug, store, agent, now_ms):
        return "its pod never started"
    if not tick.reports.host(agent):
        return ""
    if hives.gone(agent) or runtime.observe(agent).state == "exited":
        return "its hive pane is gone"
    return "its hive stopped reporting" if tick._hive_lost(slug, store, agent, now_ms) else ""


def _unbound_past_window(slug: str, store: Any, agent: AgentRecord, now_ms: int) -> bool:
    """Start the unbound window of a suspect Kubernetes attempt on the placeholder pod with no pod uid, and answer
    True once that window has lasted the lost window; the store clears the window when it retires the agent."""
    from scripts.swarm_v2.kubernetes.failures import PENDING_POD

    target = cast(dict[str, Any], agent.runtime_target)
    if agent.runtime_backend != "kubernetes" or agent.state != tick.SUSPECT:
        return False
    if target.get("pod_uid") or target.get("pod_name") != PENDING_POD:
        return False
    key = store.key(slug, "unbound-since")
    store.redis.hsetnx(key, agent.name, now_ms)
    return now_ms - int(store.redis.hget(key, agent.name)) >= tick.reports.LOST_MS


def _hive_lost(slug: Any, store: Any, agent: Any, now_ms: Any) -> bool:
    if agent.state == "finished":
        return tick.reports.Reports(store.redis).lost(agent, now_ms, tick.registry.LIVE_MS)
    if not tick.reports.Reports(store.redis).lost(agent, now_ms, tick.reports.LOST_MS):
        return False
    beat = tick.idle_state.heartbeat(store.redis, slug, agent.name)
    wait = _holding_wait(store, slug, tick.idle_state.wait(store.redis, slug, agent.name), now_ms)
    fresh = beat is not None and now_ms - beat["at"] <= tick.reports.LOST_MS
    return not fresh and (wait is None or wait["until"] <= now_ms)


def _retire_ended(slug: Any, store: Any, ledger: Any, runtime: Any, rows: Any, agent: Any, now_ms: Any) -> list[str]:
    tick.master_retire.forget(store, slug, agent.name)
    retired = store.key(slug, "retired-process")
    removed = (
        tick.reports.Reports(store.redis).removed(agent, now_ms)
        or tick.retire_watch.reported_gone(store, agent, now_ms)
        or store.redis.hexists(retired, agent.name)
    )
    if not removed and not runtime.retire(agent, homes=tick.reaper.scratch_homes(slug, agent.task)):
        tick.retire_watch.failed(store, slug, agent.name, runtime.refusal(agent), now_ms)
        if not _handed_on(slug, store, agent, now_ms):
            return [f"could not retire {agent.name}, retrying next tick"]
    store.redis.hset(retired, agent.name, now_ms)
    tick._refund_parked(slug, store, rows, agent)
    store.release(slug, agent.task, agent.name)
    if store.drop_agent(slug, agent.name) is False:
        from scripts.tick.steps.common import DROP_REFUSED

        return [f"kept {agent.name}{DROP_REFUSED}"]
    row = rows.get(agent.task, {})
    goes_on = (
        tick._current_owner(agent, row)
        and bool(store.handoff(slug, agent.task))
        and row.get("state") in tick.ACTIVE
        and not tick.awaiting_merge(row)
    )
    if goes_on:
        goes_on = tick._reopen(slug, ledger, rows, agent.task)
    tick.exits.settle(tick.InboxStore(store.redis), agent.name, agent.seat if goes_on else "", "exited")
    return [f"retired {agent.name}"]


def _handed_on(slug: str, store: Any, agent: AgentRecord, now_ms: int) -> bool:
    """True for a finished master whose seat holds a later agent and whose retire has failed for the lost window."""
    if agent.lane != tick.MASTER or agent.state != "finished":
        return False
    later = any(
        other.seat == agent.seat and other.name != agent.name and other.started_at > agent.started_at
        for other in store.agents(slug)
    )
    raw = store.redis.hget(store.key(slug, "retire-failures"), agent.name)
    return later and bool(raw) and now_ms - json.loads(raw)["since"] >= tick.reports.LOST_MS


def _live_names(runtime: Any) -> Any:
    return runtime.live_names()


def _observe(runtime: Any, agent: Any) -> Any:
    return runtime.observe(agent)


def _elsewhere(agent: Any) -> bool:
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
        or (agent.lane == tick.MASTER and agent.state == "starting")
        or tick.timing.timed(tick._acknowledging, slug, store, agent)
    ):
        return []
    if reason := tick.timing.timed(tick._remote_gone_reason, slug, store, runtime, agent, now_ms):
        if not tick._ended(agent, rows) and tick.held_elsewhere.placed(slug, store, agent, now_ms):
            store.refresh(slug, agent.task, agent.name, tick.LEASE_MS)
            return []
        return [tick.timing.timed(tick._drop_remote, slug, store, ledger, rows, agent, reason)]
    ended = tick._ended(agent, rows)
    if agent.state == "retiring" and not ended:
        return []
    if ended:
        return tick.timing.timed(tick._retire_ended, slug, store, ledger, runtime, rows, agent, now_ms)
    if agent.runtime_backend != "local" or tick.reports.host(agent) or tick._elsewhere(agent):
        return tick.timing.timed(tick._watch_remote, slug, store, ledger, runtime, rows, agent, now_ms)
    if agent.name in live and agent.lane == tick.MASTER:
        runtime.name_pane(agent)
        return tick.timing.timed(tick._watch_agent, slug, store, ledger, runtime, rows, agent, now_ms)
    if agent.name in live:
        tick.timing.timed(tick._restore_owner, slug, store, ledger, rows, agent)
        if tick._ended(agent, rows):
            return tick.timing.timed(tick._retire_ended, slug, store, ledger, runtime, rows, agent, now_ms)
        return tick.timing.timed(tick._watch_agent, slug, store, ledger, runtime, rows, agent, now_ms)
    if tick.resume.recovery_pending(store, slug, agent):
        store.refresh(slug, agent.task, agent.name, tick.LEASE_MS)
        return []
    if now_ms - agent.started_at > tick.STARTUP_GRACE_MS:
        return tick.timing.timed(tick._lost, slug, store, ledger, runtime, rows, agent, now_ms)
    return []


def _reap_admitted(judging: _Reaping, agent: AgentRecord) -> list[str]:
    """Return the actions the live agent watch takes on one agent whose hive recorded its start; its exit and its lease
    stay with the hub lifecycle."""
    slug, store = judging.slug, judging.store
    if (
        agent.state in {"awaiting-decision", "retiring"}
        or (agent.lane == tick.MASTER and agent.state == "starting")
        or tick.timing.timed(tick._acknowledging, slug, store, agent)
        or tick._ended(agent, judging.rows)
    ):
        return []
    return tick.timing.timed(
        tick._watch_agent, slug, store, judging.ledger, judging.runtime, judging.rows, agent, judging.now_ms
    )


def _lost(slug: Any, store: Any, ledger: Any, runtime: Any, rows: Any, agent: Any, now_ms: Any) -> list[str]:
    if tick.deliberate_exit.take(store, slug, agent.name):
        runtime.retire(agent, homes=tick.reaper.scratch_homes(slug, agent.task))
        return [
            f"retired {agent.name} after a deliberate terminate"
            + tick._drop(slug, store, ledger, rows, agent, refund=True)
        ]
    if tick.dead_resume.reopen(slug, store, ledger, runtime, agent, now_ms):
        return [f"resumed {agent.name}"]
    if agent not in store.agents(slug):
        return []
    runtime.retire(agent, homes=tick.reaper.scratch_homes(slug, agent.task))
    return [f"lost {agent.name}" + tick._drop(slug, store, ledger, rows, agent)]


def _watch_remote(slug: Any, store: Any, ledger: Any, runtime: Any, rows: Any, agent: Any, now_ms: Any) -> list[str]:
    """A remote agent is judged by its own runtime, never by this host's process table: no answer keeps it suspect,
    holding its claim, neither retired nor dropped."""
    tick.timing.timed(store.refresh, slug, agent.task, agent.name, tick.LEASE_MS)
    if runtime.observe(agent).state == "unknown":
        if agent.state == tick.SUSPECT:
            return []
        tick.timing.timed(store.put_agent, slug, tick.replace(agent, state=tick.SUSPECT))
        return [f"suspect {agent.name}: its runtime did not answer"]
    if agent.state == tick.SUSPECT:
        agent = tick.replace(agent, state="working")
        tick.timing.timed(store.put_agent, slug, agent)
    return tick.timing.timed(tick._watch_agent, slug, store, ledger, runtime, rows, agent, now_ms)


def _refusal_due(slug: Any, store: Any, rows: Any, agent: Any, observed: Any, now_ms: Any) -> bool:
    if not observed.content_refusal:
        return False
    row = rows.get(agent.task)
    if not row or row.get("claimed_by") != agent.name or tick._operator_at_pane(slug, store, agent, observed, now_ms):
        return False
    return tick.content_refusal.repeated(store, slug, agent, observed)


def _free_refused(slug: Any, store: Any, ledger: Any, runtime: Any, rows: Any, agent: Any, now_ms: Any) -> list[str]:
    from scripts.hub.reconciler import stop_admitted

    if not tick.retire_watch.retire_unless_gone(
        store, runtime, agent, tick.reaper.scratch_homes(slug, agent.task), now_ms
    ):
        return [f"could not retire {agent.name} after repeated content refusals, retrying next tick"]
    tick.content_refusal.handoff(store, slug, agent, now_ms)
    dropped = tick._stop_hive(slug, agent, "refused", tick._drop(slug, store, ledger, rows, agent, refund=True))
    return [f"freed {agent.name} after repeated content refusals" + dropped]


def _free_revoked(slug: Any, store: Any, ledger: Any, runtime: Any, rows: Any, agent: Any, now_ms: Any) -> list[str]:
    from scripts.hub.reconciler import stop_admitted

    account = f"{agent.harness} account {agent.account}"
    if agent.lane == tick.MASTER:
        tick.revoked_seats.recycle_master(slug, store, ledger, agent, now_ms)
        return [f"recycled master {agent.name} in its seat: {account} was revoked"]
    if not tick.retire_watch.retire_unless_gone(
        store, runtime, agent, tick.reaper.scratch_homes(slug, agent.task), now_ms
    ):
        return [f"could not retire {agent.name} on revoked {account}, retrying next tick"]
    tick.revoked_seats.worker_handoff(store, slug, agent, now_ms)
    dropped = tick._stop_hive(slug, agent, "revoked", tick._drop(slug, store, ledger, rows, agent, refund=True))
    return [f"freed the seat of {agent.name}: {account} was revoked" + dropped]


def _watch_agent(slug: Any, store: Any, ledger: Any, runtime: Any, rows: Any, agent: Any, now_ms: Any) -> list[str]:
    if tick.timing.timed(tick.revoked_seats.on_revoked, agent):
        return tick.timing.timed(tick._free_revoked, slug, store, ledger, runtime, rows, agent, now_ms)
    return tick._watch_idle(slug, store, ledger, runtime, rows, agent, now_ms)


def _watch_idle(
    slug: str,
    store: Any,
    ledger: Any,
    runtime: Any,
    rows: dict[str, dict[str, Any]],
    agent: Any,
    now_ms: int,
) -> list[str]:
    automated_gc: Any = import_module("scripts.swarm.automated_gc")

    observed = tick.timing.timed(tick._observe, runtime, agent)
    if tick.timing.timed(tick._refusal_due, slug, store, rows, agent, observed, now_ms):
        return tick.timing.timed(tick._free_refused, slug, store, ledger, runtime, rows, agent, now_ms)
    tick.timing.timed(
        store.redis.set, store.key(slug, "pane-state", agent.name), observed.state, ex=tick.idle_state.BEAT_TTL_S
    )
    tick.timing.timed(tick.outage.mark_pane, store, slug, agent.name, observed.provider_error, now_ms)
    if observed.provider_error and not observed.prompt_title and not observed.typed:
        observed = tick.replace(observed, state=tick.idle_state.IDLE)
    if observed.state == tick.idle_state.WAITING:
        title = observed.prompt_title or "Waiting on input"
        same = agent.input_prompt == title
        began = tick.timing.timed(
            tick.input_wait.mark, store, slug, agent.name, now_ms, agent.input_ticks if same else 0
        )
        ticks = tick.elapsed.passes(now_ms - began)
        waiting = tick.replace(agent, input_prompt=title, input_ticks=ticks, idle_ticks=0)
        tick.timing.timed(store.put_agent, slug, waiting)
        return (
            tick.timing.timed(tick.master_questions.dismiss, slug, store, ledger, runtime, waiting, now_ms)
            if agent.lane == tick.MASTER
            else []
        )
    if agent.input_prompt:
        tick.timing.timed(tick.input_wait.clear, store, slug, agent.name)
        if agent.lane == tick.MASTER:
            tick.timing.timed(tick.master_questions.drop, store, slug, agent.name)
    agent = tick.replace(agent, input_prompt="", input_ticks=0)
    held = tick.timing.timed(tick.outage.held, store, slug, agent.name)
    exhausted = tick.timing.timed(tick.resume.recovery_exhausted, store, slug, agent)
    pending = tick.timing.timed(tick.resume.recovery_pending, store, slug, agent)
    if tick.timing.timed(tick._stuck_on_provider, slug, store, agent, observed, exhausted, now_ms):
        return tick.timing.timed(tick._free_stuck, slug, store, ledger, runtime, rows, agent, now_ms)
    claimant = rows.get(agent.task, {}).get("claimed_by") == agent.name
    nudged: list[str] = tick._stale_nudge(slug, store, agent, observed, pending, now_ms) if claimant else []
    if agent.lane == tick.MASTER or (held and not exhausted):
        tick.timing.timed(tick.elapsed.hold, store, slug, tick.elapsed.IDLE_SINCE, agent.name, agent.idle_ticks)
        tick.timing.timed(store.put_agent, slug, agent)
        return nudged
    state = tick.idle_state.verdict(
        observed.state,
        tick.timing.timed(tick.idle_state.heartbeat, store.redis, slug, agent.name),
        _holding_wait(store, slug, tick.timing.timed(tick.idle_state.wait, store.redis, slug, agent.name), now_ms),
        now_ms,
    )
    if state in {tick.idle_state.WAITING, tick.idle_state.WORKING}:
        tick.timing.timed(store.put_agent, slug, tick.replace(agent, idle_ticks=0))
        return nudged
    if (
        tick.timing.timed(tick._operator_at_pane, slug, store, agent, observed, now_ms)
        or tick.timing.timed(tick.ledger_probe.holding, store, slug)
        or tick.timing.timed(automated_gc.owned, slug, agent)
    ):
        tick.timing.timed(tick.elapsed.hold, store, slug, tick.elapsed.IDLE_SINCE, agent.name, agent.idle_ticks)
        tick.timing.timed(store.put_agent, slug, agent)
        return nudged
    since = tick.timing.timed(
        tick.elapsed.start, store, slug, tick.elapsed.IDLE_SINCE, agent.name, now_ms, agent.idle_ticks
    )
    idle = tick.replace(agent, idle_ticks=tick.elapsed.passes(now_ms - since))
    tick.timing.timed(store.put_agent, slug, idle)
    who = tick.Who(name=agent.name, task=agent.task)
    tick.timing.timed(
        tick.gate_log.append,
        slug,
        tick.gate_log.Row.of(tick.IDLE_TICKS, "count", who, reason=f"idle tick {idle.idle_ticks}", now_ms=now_ms),
    )
    if tick.timing.timed(tick.resume.recovery_holds, store, slug, agent, now_ms):
        return nudged
    if idle.idle_ticks >= tick.IDLE_KILL_TICKS and runtime.retire(
        idle, homes=tick.reaper.scratch_homes(slug, idle.task)
    ):
        why = (
            f"; outage recovery could not proceed: {tick.timing.timed(tick.resume.recovery_blocker, store, slug, agent)}"
            if pending
            else ""
        )
        dropped = tick.timing.timed(tick._drop, slug, store, ledger, rows, idle, refund=True)
        tick.timing.timed(tick._stop_hive, slug, idle, "idle", dropped)
        return nudged + [f"stalled {agent.name}" + dropped + why]
    return nudged


WAITS_END = "waits end"


def _holding_wait(store: Any, slug: str, found: Any, now_ms: int) -> Any:
    """The declared wait ``found`` as the idle count reads it: a checked wait only while the waits end step, which
    ends a checked wait once its target resolves, has run within the idle retirement window."""
    if not isinstance(found, dict):
        return found
    held = cast(dict[str, Any], found)
    if not held.get("on"):
        return held
    ends: dict[str, float] = tick.timing.step_ends(store, slug)
    ended = ends.get(WAITS_END)
    if ended is None or now_ms - int(ended * 1000) < tick.IDLE_KILL_MS:
        return held
    return None


def _stale_nudge(slug: str, store: Any, agent: AgentRecord, observed: Any, pending: bool, now_ms: int) -> list[str]:
    """An open swarm outage and a pending recovery belong to provider recovery; an agent due for retirement gets none."""
    if agent.lane == tick.MASTER or not agent.task or pending or tick.timing.timed(tick.outage.open_records, slug):
        return []
    if tick.timing.timed(
        tick.elapsed.due, store, slug, tick.elapsed.IDLE_SINCE, agent.name, now_ms, agent.idle_ticks, tick.IDLE_KILL_MS
    ):
        return []
    if tick.timing.timed(tick._operator_at_pane, slug, store, agent, observed, now_ms) or tick.timing.timed(
        tick.ledger_probe.holding, store, slug
    ):
        return []
    return tick.stale_nudge.nudge(tick.InboxStore(store.redis), slug, agent, observed.provider_error, now_ms)


def _stuck_on_provider(slug: Any, store: Any, agent: Any, observed: Any, exhausted: Any, now_ms: Any) -> bool:
    if (
        agent.lane == tick.MASTER
        or not observed.provider_error
        or tick._operator_at_pane(slug, store, agent, observed, now_ms)
    ):
        return False
    if exhausted:
        return True
    state = tick.idle_state.verdict(
        observed.state,
        tick.idle_state.heartbeat(store.redis, slug, agent.name),
        _holding_wait(store, slug, tick.idle_state.wait(store.redis, slug, agent.name), now_ms),
        now_ms,
    )
    return state == tick.idle_state.IDLE and tick.outage.held_past_limit(store, slug, agent.name, now_ms)


def _free_stuck(slug: Any, store: Any, ledger: Any, runtime: Any, rows: Any, agent: Any, now_ms: Any) -> list[str]:
    from scripts.hub.reconciler import stop_admitted

    if not tick.retire_watch.retire_unless_gone(
        store, runtime, agent, tick.reaper.scratch_homes(slug, agent.task), now_ms
    ):
        store.put_agent(slug, agent)
        return [f"could not retire {agent.name}, stuck on a provider error, retrying next tick"]
    tick.resume.free_seat(store, slug, agent, now_ms)
    tick.outage.clear_pane(store, slug, agent.name)
    dropped = tick._stop_hive(slug, agent, "stuck", tick._drop(slug, store, ledger, rows, agent, refund=True))
    return [f"freed the seat of {agent.name}, stuck on a provider error" + dropped]


def _operator_at_pane(slug: Any, store: Any, agent: Any, observed: Any, now_ms: Any) -> bool:
    """The idle count holds while the pane's input line holds text or the operator prompted it inside the quiet window."""
    last = tick.idle_state.last_prompt(store.redis, slug, agent.name)
    return bool(observed.typed) or (last is not None and now_ms - last < tick.wake.quiet_ms(tick.os.environ))


def _conversations(slug: str, store: Any, runtime: Any) -> None:
    """Record each paned agent's current conversation, fanned out over the pass workers."""
    local = runtime.conversations()
    found: dict[str, str] = {**(local or {}), **tick.reports.Reports(store.redis).conversations()}

    def one(agent: AgentRecord) -> None:
        """Pin and record the conversation the pane of ``agent`` reports."""
        if agent.state in {"finished", "awaiting-decision"} or not agent.pane_id:
            return
        pane = tick.reports.pane_key(agent)
        if pane not in found and (
            local is None or tick.reports.host(agent) or tick.resume.recovery_pending(store, slug, agent)
        ):
            return
        current = found.get(pane, "")
        if current:
            tick.context_recycle.pin_swarm(current, slug, store.config(slug).compact_limit or None)
        if current and current != agent.conversation_id:
            store.put_agent(slug, tick.replace(agent, conversation_id=current))
            store.names.note(agent.name, session_id=current)

    tick.pool.fan_out(one, store.agents(slug))


def _session_models(slug: Any, store: Any) -> Any:
    for agent in store.agents(slug):
        reported = tick.session_model.apply(agent, tick.session_model.get(store.redis, slug, agent.name))
        if reported != agent:
            store.put_agent(slug, reported)


def _reap_step(tick: Any, get: Get) -> Any:
    tools, deadline = get("tools"), get(DEADLINE)
    return tick.skip_refused(
        _reap,
        get("slug"),
        get("store"),
        LedgerCalls(tools.ledger, deadline),
        RuntimeCalls(tools.runtime, deadline),
        get("rows"),
        get("now_ms"),
        deadline,
    )


def _recover_outages(tick: Any, get: Get) -> Any:
    return tick.skip_refused(
        tick.resume.recover_outages,
        get("slug"),
        get("store"),
        get("ledger"),
        get("runtime"),
        get("rows"),
        get("now_ms"),
    )


def _strays_step(tick: Any, get: Get) -> Any:
    return tick.skip_refused(_strays, get("slug"), get("config"), get("store"), get("runtime"))


def _failed_panes(tick: Any, get: Get) -> Any:
    return tick.skip_refused(tick.failed_panes.sweep, get("slug"), get("store"), get("runtime"), get("now_ms"))


def _orphans_step(tick: Any, get: Get) -> Any:
    return tick.skip_refused(_orphans, get("slug"), get("store"), get("ledger"), get("rows"))


def _conversations_step(tick: Any, get: Get) -> Any:
    return tick.skip_refused(_conversations, get("slug"), get("store"), get("runtime"))


def _session_models_step(tick: Any, get: Get) -> Any:
    return tick.skip_refused(_session_models, get("slug"), get("store"))


def _transfers(tick: Any, get: Get) -> Any:
    return tick.skip_refused(tick.transfers.observe, get("store"), get("slug"), get("runtime").live_names())


LISTED = frozenset({"spawn due"})
ROWS = pass_domains("rows")

STEPS = (
    CallStep(
        "reap",
        "agents",
        _reap_step,
        LISTED,
        frozenset({AGENTS}) | ROWS,
        frozenset({AGENTS, TASKS}),
        lane=CallStep.lane,
        effects=frozenset({"ledger", "runtime", "inbox"}),
        feature="swarm-tick-agents-reap",
    ),
    CallStep("recover outages", "agents", _recover_outages, LISTED, ROWS),
    CallStep("strays", "agents", _strays_step, frozenset({"drained resume"}), pass_domains("config"), lane=SLOW),
    CallStep("failed panes", "agents", _failed_panes, lane=SLOW),
    CallStep(
        "orphans",
        "tasks",
        _orphans_step,
        frozenset({"reap", "drained resume"}),
        frozenset({TASKS}) | pass_domains("config", "rows"),
        frozenset({TASKS}),
        feature="swarm-tick-events-orphans",
    ),
    CallStep("conversations", "close", _conversations_step, lane=SLOW),
    CallStep("session models", "close", _session_models_step, lane=SLOW),
    CallStep("handoff transfers", "close", _transfers),
)

for _step in STEPS:
    registry.register(_step)

tick: Any = import_module("scripts.swarm.tick")
