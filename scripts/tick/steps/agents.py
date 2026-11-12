"""The agent steps of the tick pass: reap, outages, strays, failed panes, orphans and session upkeep."""

# pyright: reportUnusedFunction=false

from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from typing import Any

from scripts.swarm.store import AgentRecord, RedisStore
from scripts.tick import registry
from scripts.tick.steps import AGENTS, TASKS, CallStep, pass_domains

Get = Callable[[str], Any]


def _reap(slug: str, store: Any, ledger: Any, runtime: Any, rows: dict[str, dict[str, Any]], now_ms: int) -> list[str]:
    """Judge every legacy agent of the swarm, one task per pass worker, returning actions in agent order."""
    from scripts.hub.reconciler import legacy_agents

    live, rows = tick.timing.timed(tick._live_names, runtime), dict(rows)

    judging = tick._Reaping(slug, store, ledger, runtime, rows, live, now_ms)
    agents = list(tick.timing.timed(legacy_agents, store, slug))
    by_task: dict[str, list[int]] = {}
    for index, agent in enumerate(agents):
        by_task.setdefault(agent.task or agent.name, []).append(index)

    def one(indexes: list[int]) -> list[tuple[int, list[str]]]:
        """Return the actions judging each agent of one task took, in agent order."""
        judged: list[tuple[int, list[str]]] = []
        for index in indexes:
            with tick.timing.acting(agents[index].name):
                judged.append((index, tick._reap_agent(judging, agents[index])))
        return judged

    found = dict(pair for pairs in tick.pool.fan_out(one, by_task.values()) for pair in pairs)
    return [action for index in range(len(agents)) for action in found[index]]


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
            if tick._reopen(slug, ledger, rows, task_id):
                actions.append(f"task {task_id} had no agent, reopened")
    return actions


def _restore_owner(slug: Any, store: Any, ledger: Any, rows: Any, agent: Any) -> None:
    row = rows.get(agent.task, {})
    if (
        tick._ended(agent, rows)
        or not tick._current_owner(agent, row)
        or not store.ensure_claim(slug, agent, tick.LEASE_MS)
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
    rows[agent.task] = ledger.update_task(slug, agent.task, fields, if_state=(row["state"],), if_match=expected)


def _drop_remote(slug: Any, store: Any, ledger: Any, rows: Any, agent: Any, reason: Any) -> str:
    tick.master_retire.forget(store, slug, agent.name)
    tick._refund_parked(slug, store, rows, agent)
    return f"retired {agent.name}: {reason}" + tick._drop(slug, store, ledger, rows, agent)


def _remote_gone_reason(slug: str, store: Any, runtime: Any, agent: AgentRecord, now_ms: int) -> str:
    hives = tick.reports.Reports(store.redis)
    if tick._elsewhere(agent) and hives.vanished(agent, now_ms):
        return "its hive no longer lists it"
    if not tick.reports.host(agent):
        return ""
    if hives.gone(agent) or runtime.observe(agent).state == "exited":
        return "its hive pane is gone"
    return "its hive stopped reporting" if tick._hive_lost(slug, store, agent, now_ms) else ""


def _hive_lost(slug: Any, store: Any, agent: Any, now_ms: Any) -> bool:
    if agent.state == "finished":
        return tick.reports.Reports(store.redis).lost(agent, now_ms, tick.registry.LIVE_MS)
    if not tick.reports.Reports(store.redis).lost(agent, now_ms, tick.reports.LOST_MS):
        return False
    beat = tick.idle_state.heartbeat(store.redis, slug, agent.name)
    wait = tick.idle_state.wait(store.redis, slug, agent.name)
    fresh = beat is not None and now_ms - beat["at"] <= tick.reports.LOST_MS
    return not fresh and (wait is None or wait["until"] <= now_ms)


def _retire_ended(slug: Any, store: Any, ledger: Any, runtime: Any, rows: Any, agent: Any, now_ms: Any) -> list[str]:
    tick.master_retire.forget(store, slug, agent.name)
    removed = tick.reports.Reports(store.redis).removed(agent, now_ms)
    if not removed and not runtime.retire(agent, homes=tick.reaper.scratch_homes(slug, agent.task)):
        tick.retire_watch.failed(store, slug, agent.name, runtime.refusal(agent), now_ms)
        return [f"could not retire {agent.name}, retrying next tick"]
    tick._refund_parked(slug, store, rows, agent)
    store.release(slug, agent.task, agent.name)
    store.drop_agent(slug, agent.name)
    row = rows.get(agent.task, {})
    goes_on = bool(store.handoff(slug, agent.task)) and row.get("state") in tick.ACTIVE and not tick.awaiting_merge(row)
    if goes_on:
        tick._reopen(slug, ledger, rows, agent.task)
    tick.exits.settle(tick.InboxStore(store.redis), agent.name, agent.seat if goes_on else "", "exited")
    return [f"retired {agent.name}"]


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
    store.refresh(slug, agent.task, agent.name, tick.LEASE_MS)
    if runtime.observe(agent).state == "unknown":
        if agent.state == tick.SUSPECT:
            return []
        store.put_agent(slug, tick.replace(agent, state=tick.SUSPECT))
        return [f"suspect {agent.name}: its runtime did not answer"]
    if agent.state == tick.SUSPECT:
        agent = tick.replace(agent, state="working")
        store.put_agent(slug, agent)
    return tick._watch_agent(slug, store, ledger, runtime, rows, agent, now_ms)


def _refusal_due(slug: Any, store: Any, rows: Any, agent: Any, observed: Any, now_ms: Any) -> bool:
    if not observed.content_refusal:
        return False
    row = rows.get(agent.task)
    if not row or row.get("claimed_by") != agent.name or tick._operator_at_pane(slug, store, agent, observed, now_ms):
        return False
    return tick.content_refusal.repeated(store, slug, agent, observed)


def _free_refused(slug: Any, store: Any, ledger: Any, runtime: Any, rows: Any, agent: Any, now_ms: Any) -> list[str]:
    if not runtime.retire(agent, homes=tick.reaper.scratch_homes(slug, agent.task)):
        return [f"could not retire {agent.name} after repeated content refusals, retrying next tick"]
    tick.content_refusal.handoff(store, slug, agent, now_ms)
    return [
        f"freed {agent.name} after repeated content refusals"
        + tick._drop(slug, store, ledger, rows, agent, refund=True)
    ]


def _free_revoked(slug: Any, store: Any, ledger: Any, runtime: Any, rows: Any, agent: Any, now_ms: Any) -> list[str]:
    account = f"{agent.harness} account {agent.account}"
    if agent.lane == tick.MASTER:
        tick.revoked_seats.recycle_master(slug, store, ledger, agent, now_ms)
        return [f"recycled master {agent.name} in its seat: {account} was revoked"]
    if not runtime.retire(agent, homes=tick.reaper.scratch_homes(slug, agent.task)):
        return [f"could not retire {agent.name} on revoked {account}, retrying next tick"]
    tick.revoked_seats.worker_handoff(store, slug, agent, now_ms)
    return [
        f"freed the seat of {agent.name}: {account} was revoked"
        + tick._drop(slug, store, ledger, rows, agent, refund=True)
    ]


def _watch_agent(slug: Any, store: Any, ledger: Any, runtime: Any, rows: Any, agent: Any, now_ms: Any) -> list[str]:
    if tick.revoked_seats.on_revoked(agent):
        return tick._free_revoked(slug, store, ledger, runtime, rows, agent, now_ms)
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
        return tick._free_refused(slug, store, ledger, runtime, rows, agent, now_ms)
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
        return tick._free_stuck(slug, store, ledger, runtime, rows, agent, now_ms)
    claimant = rows.get(agent.task, {}).get("claimed_by") == agent.name
    nudged: list[str] = tick._stale_nudge(slug, store, agent, observed, pending, now_ms) if claimant else []
    if agent.lane == tick.MASTER or (held and not exhausted):
        tick.timing.timed(tick.elapsed.hold, store, slug, tick.elapsed.IDLE_SINCE, agent.name, agent.idle_ticks)
        tick.timing.timed(store.put_agent, slug, agent)
        return nudged
    state = tick.idle_state.verdict(
        observed.state,
        tick.timing.timed(tick.idle_state.heartbeat, store.redis, slug, agent.name),
        tick.timing.timed(tick.idle_state.wait, store.redis, slug, agent.name),
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
            f"; outage recovery could not proceed: {tick.resume.recovery_blocker(store, slug, agent)}"
            if pending
            else ""
        )
        return nudged + [f"stalled {agent.name}" + tick._drop(slug, store, ledger, rows, idle, refund=True) + why]
    return nudged


def _stale_nudge(slug: str, store: Any, agent: AgentRecord, observed: Any, pending: bool, now_ms: int) -> list[str]:
    """An open swarm outage and a pending recovery belong to provider recovery; an agent due for retirement gets none."""
    if agent.lane == tick.MASTER or not agent.task or pending or tick.timing.timed(tick.outage.open_records, slug):
        return []
    if tick.elapsed.due(store, slug, tick.elapsed.IDLE_SINCE, agent.name, now_ms, agent.idle_ticks, tick.IDLE_KILL_MS):
        return []
    if tick._operator_at_pane(slug, store, agent, observed, now_ms) or tick.ledger_probe.holding(store, slug):
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
        tick.idle_state.wait(store.redis, slug, agent.name),
        now_ms,
    )
    return state == tick.idle_state.IDLE and tick.outage.held_past_limit(store, slug, agent.name, now_ms)


def _free_stuck(slug: Any, store: Any, ledger: Any, runtime: Any, rows: Any, agent: Any, now_ms: Any) -> list[str]:
    if not runtime.retire(agent, homes=tick.reaper.scratch_homes(slug, agent.task)):
        store.put_agent(slug, agent)
        return [f"could not retire {agent.name}, stuck on a provider error, retrying next tick"]
    tick.resume.free_seat(store, slug, agent, now_ms)
    tick.outage.clear_pane(store, slug, agent.name)
    return [
        f"freed the seat of {agent.name}, stuck on a provider error"
        + tick._drop(slug, store, ledger, rows, agent, refund=True)
    ]


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
    return tick.skip_refused(
        _reap, get("slug"), get("store"), get("ledger"), get("runtime"), get("rows"), get("now_ms")
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
    if get("config").engine == tick.EVENTS_ENGINE:
        return []
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
    CallStep("strays", "agents", _strays_step, frozenset({"stopped branch"}), pass_domains("config")),
    CallStep("failed panes", "agents", _failed_panes),
    CallStep(
        "orphans",
        "tasks",
        _orphans_step,
        frozenset({"reap", "drained resume"}),
        frozenset({TASKS}) | pass_domains("config", "rows"),
        frozenset({TASKS}),
    ),
    CallStep("conversations", "close", _conversations_step),
    CallStep("session models", "close", _session_models_step),
    CallStep("handoff transfers", "close", _transfers),
)

for _step in STEPS:
    registry.register(_step)

tick: Any = import_module("scripts.swarm.tick")
