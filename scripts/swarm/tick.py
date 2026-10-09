"""One reconcile pass over a swarm: retire finished agents, free dead or stalled agents' tasks, spawn up to the caps.

Scaling up is immediate; scaling down happens only as agents finish, so a lowered cap never kills work.
Each swarm keeps at most one master: an agent the operator talks to, which works no task.
"""

import json
import os
import sys
import threading
from dataclasses import dataclass, field, replace
from itertools import count
from typing import Protocol

from scripts.doctor import priming
from scripts.gates import Who, modes
from scripts.gates import claims as claim_cap
from scripts.gates import log as gate_log
from scripts.handoff import transfers
from scripts.handoff.envelope import reclaim
from scripts.inbox import exits, wake
from scripts.inbox.seats import seat_address
from scripts.inbox.store import CLOSED, InboxStore
from scripts.swarm import (
    affinity,
    ci_speed,
    claim_order,
    control_notifications,
    difficulty,
    grouping,
    launch_check,
    lifetime,
    live_binding,
    master_retire,
    master_start,
    phase_state,
    reaper,
    retire_watch,
    session_model,
    tick_master,
    time_left,
    timing,
)
from scripts.swarm import idle as idle_state
from scripts.swarm.ledger_client import LedgerRefused
from scripts.swarm.naming import parse
from scripts.swarm.pane import PaneObservation
from scripts.swarm.profile_choice import ProfileUnresolved
from scripts.swarm.store import MASTER, PREFIX, AgentRecord, SwarmConfig
from scripts.swarm_ledger import ledger_rank, ledger_workspace

LEASE_MS = 10 * 60 * 1000
STARTUP_GRACE_MS = 6 * 60 * 1000
SUSPECT = "suspect"
MASTER_WAITING = f"{PREFIX}:master-waiting"
MASTER_WAIT_MS = 10 * 60 * 1000
# Swarms tick in threads; two placing from one live session count overfill an account.
PLACING = threading.Lock()
DOWN_TOLD = "master down told"
REDELIVERED = "the master went down before closing it; kept for the next master"
MASTER_DOWN = (
    "The master is down and is being relaunched. Your message waits for the new master, which receives it as soon "
    "as it starts."
)
IDLE_NUDGE_TICKS = 3
IDLE_KILL_TICKS = 10
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
    def __init__(self, message: str, status: str = "refused"):
        super().__init__(message)
        self.status = status


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


class Ledger(Protocol):
    def state(self, slug: str) -> dict: ...
    def update_task(self, slug: str, task_id: str, fields: dict, by: str = ..., if_state: tuple = ...) -> dict: ...
    def comment(self, slug: str, task_id: str, text: str, by: str) -> None: ...
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
    def nudge(self, agent: AgentRecord, text: str) -> None: ...
    def name_pane(self, agent: AgentRecord) -> bool: ...
    def conversations(self) -> dict[str, str] | None: ...
    def resume(self, config, agent: AgentRecord, text: str) -> Placed: ...
    def close_space(self, config: SwarmConfig) -> bool: ...


def tick(slug, store, ledger, runtime, now_ms):
    config = store.ensure_code(slug)
    actions = []
    if config.state != "stopped" or _woken(slug, config, store, ledger):
        actions = skip_refused(_recover_master, slug, config, store, runtime, now_ms)
    doc = timing.call(ledger.state, slug)
    rows = {t["id"]: t for t in doc["tasks"]}
    timing.call(
        exits.sweep,
        InboxStore(store.redis),
        slug,
        store,
        lambda: {t["id"]: t for t in timing.call(ledger.state, slug)["tasks"]},
    )
    actions += skip_refused(master_start.observe, slug, config, store, ledger, runtime, now_ms)
    actions += skip_refused(_launch_checks, slug, store, ledger, runtime, rows, doc, now_ms)
    actions += skip_refused(_verify, slug, store, ledger, runtime, rows, now_ms)
    actions += skip_refused(_reap, slug, store, ledger, runtime, rows, now_ms)
    actions += skip_refused(_strays, slug, config, store, runtime)
    actions += skip_refused(lifetime.retire_idle_master, slug, store, ledger, runtime, rows, now_ms)
    if config.state == "stopped":
        retired = store.redis.get(store.key(slug, "master-retired-tasks")) is not None
        if not _woken(slug, config, store, ledger) and (not retired or lifetime.sleeping(slug, store, rows)):
            return (
                actions
                + timing.call(_close_space, slug, config, store, runtime)
                + _bin_closed(slug, store, ledger, doc)
            )
        config = store.update(slug, state="paused")
        actions.append("the operator wrote on the ledger, paused to start the master")
    sleeping = lifetime.sleeping(slug, store, rows)
    if not sleeping and config.state == "drained" and any(_claimable(slug, store, rows, doc, lane) for lane in LANES):
        config = store.update(slug, state="running")
        actions.append("new tasks, running again")
    actions += skip_refused(_orphans, slug, store, ledger, rows)
    actions += skip_refused(difficulty.size_pass, slug, ledger, doc)
    actions += skip_refused(grouping.release_pass, slug, store, ledger, doc)
    actions += skip_refused(grouping.group_pass, slug, config, store, ledger, doc)
    from scripts.swarm import quota_notice

    with PLACING:
        actions += skip_refused(quota_notice.refresh, slug, config, store, ledger, runtime, now_ms)
        actions += skip_refused(ci_speed.refresh, slug, config, store, now_ms)
        actions += skip_refused(time_left.refresh, slug, store, ledger, runtime, doc, now_ms)
        if not sleeping:
            actions += skip_refused(_codex_hook_order)
            actions += skip_refused(_master_down, slug, config, store, ledger, runtime, now_ms)
            actions += skip_refused(
                tick_master.run,
                slug,
                config,
                store,
                ledger,
                runtime,
                now_ms,
                lambda: _master(slug, config, store, runtime, now_ms),
            )
            if config.state == "running":
                actions += skip_refused(_spawn, slug, config, store, ledger, runtime, rows, doc, now_ms)
    timing.call(_conversations, slug, store, runtime)
    timing.call(_session_models, slug, store)
    starting = {a.name for a in store.agents(slug) if a.lane == MASTER and a.state == "starting"}
    timing.call(transfers.observe, store, slug, runtime.live_names() - starting)
    return (
        actions
        + skip_refused(_settle, slug, config, store, ledger, rows, doc)
        + timing.call(_close_space, slug, config, store, runtime)
    )


def skip_refused(function, *args):
    try:
        return timing.call(function, *args)
    except LedgerRefused as exc:
        name = f"{function.__module__}.{function.__qualname__}"
        print(f"{name} skipped, the ledger refused its write: {exc}", file=sys.stderr)
        return [f"skipped {name}: the ledger refused its write"]


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


def _operator_line(item):
    return item.sender == "operator" and not (item.fyi and item.ref.startswith(control_notifications.CONTROL_REF))


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


def _drop(slug, store, ledger, rows, agent):
    store.release(slug, agent.task, agent.name)
    store.drop_agent(slug, agent.name)
    held = rows.get(agent.task, {}).get("state") in ACTIVE and rows[agent.task].get("claimed_by") == agent.name
    reopen = held and _reopen(slug, ledger, rows, agent.task)
    exits.settle(InboxStore(store.redis), agent.name, agent.seat if reopen else "", "stopped")
    return f", task {agent.task} reopened" if reopen else ""


def _verify(slug, store, ledger, runtime, rows, now_ms):
    judged = launch_check.judged(store, slug)
    agents, actions = [a for a in store.agents(slug) if a.name not in judged], []
    facts = runtime.bindings(agents)
    for agent in agents:
        if agent.state == "awaiting-decision" or (agent.lane == MASTER and agent.state == "starting"):
            continue
        task = rows.get(agent.task, {})
        if _ended(agent, rows):
            continue
        if agent.name not in facts and agent.state != "retiring":
            continue
        agent, followed = _follow(slug, store, runtime, agent, facts.get(agent.name, {}).get("rebound"))
        actions.extend(followed)
        if agent is None:
            continue
        filled = live_binding.fill(agent, facts.get(agent.name, {}))
        if filled != agent:
            store.put_agent(slug, filled)
            agent = filled
        differences = live_binding.record(store, slug, agent, facts.get(agent.name, {"process": False}), now_ms)
        if not differences:
            if agent.state == "retiring":
                store.put_agent(slug, replace(agent, state="working"))
            continue
        fields = ", ".join(differences)
        saved = live_binding.relaunch_assignment(agent, task, store.config(slug))
        if agent.lane == MASTER and "process" not in differences and not live_binding.complete(saved):
            actions.append(f"kept {agent.name} after mismatched {fields}: its relaunch assignment is incomplete")
            continue
        live = agent.name in facts and "process" not in differences
        if held := master_retire.hold(store, slug, agent, f"mismatched {fields}", live, now_ms):
            store.redis.hset(store.key(slug, "launch-assignments"), agent.task, json.dumps(saved))
            actions.append(held)
            continue
        if not runtime.retire(agent, homes=reaper.scratch_homes(slug, agent.task)):
            store.put_agent(slug, replace(agent, state="retiring"))
            actions.append(f"could not retire {agent.name} after mismatched {fields}, retrying next tick")
            continue
        store.redis.hset(store.key(slug, "launch-assignments"), agent.task, json.dumps(saved))
        actions.append(f"retired {agent.name} after mismatched {fields}" + _drop(slug, store, ledger, rows, agent))
    return actions


def _follow(slug, store, runtime, agent, pid):
    if pid is None:
        return agent, []
    if (rebound := _rebind(slug, store, runtime, agent, pid)) is None:
        return None, [f"held {agent.name} until one pane holds its resumed process {pid}"]
    return rebound, [f"rebound {agent.name} to its resumed process {pid} in pane {rebound.pane_id}"]


def _rebind(slug, store, runtime, agent, pid):
    panes = [p for p, c in (runtime.conversations() or {}).items() if c and c == agent.conversation_id]
    if len(panes) != 1:
        return None
    validation = {**agent.profile_decision.get("validation", {}), "pid": pid}
    rebound = replace(agent, pane_id=panes[0], profile_decision={**agent.profile_decision, "validation": validation})
    store.put_agent(slug, rebound)
    return rebound


def _ended(agent, rows):
    task = rows.get(agent.task, {})
    ended = agent.lane != MASTER and (task.get("done") or task.get("state") in {"done", "blocked", "handoff"})
    return agent.state == "finished" or bool(ended)


def _launch_checks(slug, store, ledger, runtime, rows, doc, now_ms):
    waiting, actions = launch_check.pending(store, slug), []
    if not waiting:
        return actions
    agents = [a for a in store.agents(slug) if a.name in waiting]
    for name in set(waiting) - {a.name for a in agents}:
        launch_check.forget(store, slug, name)
    facts = runtime.bindings(agents)
    for agent in agents:
        found = launch_check.misses(
            store,
            slug,
            agent,
            facts.get(agent.name, {}),
            doc,
            launch_check.bundled(agent.profile),
            launch_check.declared(agent.profile, agent.overlays),
        )
        if found and now_ms - launch_check.session_started_at(agent) < launch_check.DEADLINE_MS:
            continue
        if found:
            miss = launch_check.Miss(agent, found, waiting[agent.name]["relaunch"])
            actions.append(_failed_launch(slug, store, ledger, runtime, rows, miss, now_ms))
            continue
        elapsed = launch_check.joined_at(agent, doc) - launch_check.session_started_at(agent)
        launch_check.record(store, slug, agent, found, now_ms, elapsed)
        launch_check.forget(store, slug, agent.name)
        launch_check.clear_relaunched(store, slug, agent.task)
        actions.append(f"{agent.name} passed its launch check in {elapsed // 1000} seconds")
    return actions


def _failed_launch(slug, store, ledger, runtime, rows, miss, now_ms):
    agent, found = miss.agent, miss.found
    fields, elapsed = ", ".join(found), now_ms - launch_check.session_started_at(agent)
    if not miss.relaunch or set(found) <= launch_check.REPORT_ONLY:
        outcome, said = "report", f"{agent.name} failed its launch check on {fields}; reported only"
    elif launch_check.relaunched(store, slug, agent.task):
        outcome, said = "spent", f"{agent.name} failed its launch check on {fields}; its one relaunch is spent"
    elif held := master_retire.hold(
        store, slug, agent, f"its launch check failed on {fields}", agent.name in runtime.live_names(), now_ms
    ):
        return held
    elif not runtime.retire(agent, homes=reaper.scratch_homes(slug, agent.task)):
        return f"could not retire {agent.name} after its launch check failed on {fields}, retrying next tick"
    else:
        outcome = "relaunch"
    launch_check.record(store, slug, agent, found, now_ms, elapsed, held=outcome == "spent")
    launch_check.forget(store, slug, agent.name)
    enforced = {field: values for field, values in found.items() if field not in launch_check.REPORT_ONLY}
    if agent.lane == MASTER and enforced:
        ledger.notify(slug, launch_check.told(enforced, outcome))
    if outcome != "relaunch":
        return said
    launch_check.mark_relaunched(store, slug, agent.task)
    saved = live_binding.relaunch_assignment(agent, rows.get(agent.task, {}), store.config(slug))
    store.redis.hset(store.key(slug, "launch-assignments"), agent.task, json.dumps(saved))
    pending = master_start.read(store, slug)
    if agent.lane == MASTER and pending.get("name") == agent.name:
        master_start.save(store, slug, {**pending, "name": "", "retry": True, "at": now_ms})
    return f"retired {agent.name} after its launch check failed on {fields}" + _drop(slug, store, ledger, rows, agent)


def _reap(slug, store, ledger, runtime, rows, now_ms):
    live, actions = runtime.live_names(), []
    for agent in store.agents(slug):
        if agent.state == "awaiting-decision" or (agent.lane == MASTER and agent.state == "starting"):
            continue
        ended = _ended(agent, rows)
        if agent.state == "retiring" and not ended:
            continue
        if ended:
            master_retire.forget(store, slug, agent.name)
            if runtime.retire(agent, homes=reaper.scratch_homes(slug, agent.task)):
                store.release(slug, agent.task, agent.name)
                store.drop_agent(slug, agent.name)
                _refund_parked(slug, store, rows, agent.task)
                goes_on = bool(store.handoff(slug, agent.task)) and rows.get(agent.task, {}).get("state") in ACTIVE
                if goes_on:
                    _reopen(slug, ledger, rows, agent.task)
                exits.settle(InboxStore(store.redis), agent.name, agent.seat if goes_on else "", "exited")
                actions.append(f"retired {agent.name}")
            else:
                retire_watch.failed(store, slug, agent.name, runtime.refusal(agent), now_ms)
                actions.append(f"could not retire {agent.name}, retrying next tick")
        elif agent.runtime_backend != "local":
            actions += _watch_remote(slug, store, ledger, runtime, rows, agent, now_ms)
        elif agent.name in live and agent.lane == MASTER:
            runtime.name_pane(agent)
            actions += _watch_idle(slug, store, ledger, runtime, rows, agent, now_ms)
        elif agent.name in live:
            store.refresh(slug, agent.task, agent.name, LEASE_MS)
            actions += _watch_idle(slug, store, ledger, runtime, rows, agent, now_ms)
        elif now_ms - agent.started_at > STARTUP_GRACE_MS:
            runtime.retire(agent, homes=reaper.scratch_homes(slug, agent.task))
            actions.append(f"lost {agent.name}" + _drop(slug, store, ledger, rows, agent))
    return actions


def _watch_remote(slug, store, ledger, runtime, rows, agent, now_ms):
    """A remote agent is judged by its own runtime, never by this host's process table: no answer keeps it suspect,
    holding its claim, neither retired nor dropped."""
    store.refresh(slug, agent.task, agent.name, LEASE_MS)
    if runtime.observe(agent).state == "unknown":
        if agent.state == SUSPECT:
            return []
        store.put_agent(slug, replace(agent, state=SUSPECT))
        return [f"suspect {agent.name}: its runtime did not answer"]
    if agent.state == SUSPECT:
        agent = replace(agent, state="working")
        store.put_agent(slug, agent)
    return _watch_idle(slug, store, ledger, runtime, rows, agent, now_ms)


def _strays(slug, config, store, runtime):
    """Live agent processes still carrying a name this swarm issued and retired: every holder is left over."""
    recorded, actions = {a.name for a in store.agents(slug)}, []
    for name in sorted(runtime.live_names() - recorded):
        found = parse(name)
        if not found or found.code != config.code or not store.names.entry(name).get("retired_at"):
            continue
        if runtime.reap_name(name):
            actions.append(f"reaped stray {name}")
        else:
            actions.append(f"could not reap stray {name}, retrying next tick")
    return actions


def agent_status(agent):
    if agent.state in {"finished", "awaiting-decision"}:
        return agent.state
    if agent.input_prompt:
        return "waiting"
    if agent.idle_ticks >= IDLE_NUDGE_TICKS:
        return "stalled"
    return "idle" if agent.idle_ticks else "working"


def _watch_idle(slug, store, ledger, runtime, rows, agent, now_ms):
    observed = runtime.observe(agent)
    if observed.state == idle_state.WAITING:
        title = observed.prompt_title or "Waiting on input"
        ticks = agent.input_ticks + 1 if agent.input_prompt == title else 1
        store.put_agent(slug, replace(agent, input_prompt=title, input_ticks=ticks, idle_ticks=0))
        return []
    agent = replace(agent, input_prompt="", input_ticks=0)
    if agent.lane == MASTER:
        store.put_agent(slug, agent)
        return []
    state = idle_state.verdict(
        observed.state,
        idle_state.heartbeat(store.redis, slug, agent.name),
        idle_state.wait(store.redis, slug, agent.name),
        now_ms,
    )
    if state in {idle_state.WAITING, idle_state.WORKING}:
        store.put_agent(slug, replace(agent, idle_ticks=0))
        return []
    if _operator_at_pane(slug, store, agent, observed, now_ms):
        store.put_agent(slug, agent)
        return []
    idle = replace(agent, idle_ticks=agent.idle_ticks + 1)
    store.put_agent(slug, idle)
    who = Who(name=agent.name, task=agent.task)
    gate_log.append(
        slug, gate_log.Row.of(IDLE_TICKS, "count", who, reason=f"idle tick {idle.idle_ticks}", now_ms=now_ms)
    )
    if idle.idle_ticks == IDLE_NUDGE_TICKS:
        runtime.nudge(idle, NUDGE.format(slug=slug))
        return [f"nudged {agent.name}"]
    if idle.idle_ticks >= IDLE_KILL_TICKS and runtime.retire(idle, homes=reaper.scratch_homes(slug, idle.task)):
        return [f"stalled {agent.name}" + _drop(slug, store, ledger, rows, idle)]
    return []


def _operator_at_pane(slug, store, agent, observed, now_ms):
    """The idle count holds while the pane's input line holds text or the operator prompted it inside the quiet window."""
    last = idle_state.last_prompt(store.redis, slug, agent.name)
    return bool(observed.typed) or (last is not None and now_ms - last < wake.quiet_ms(os.environ))


def _orphans(slug, store, ledger, rows):
    known = {a.name for a in store.agents(slug)}
    actions = []
    for task_id, row in rows.items():
        if row.get("state") in ACTIVE and row.get("claimed_by") not in known and store.claimant(slug, task_id) is None:
            if _reopen(slug, ledger, rows, task_id):
                actions.append(f"task {task_id} had no agent, reopened")
    return actions


def _conversations(slug, store, runtime):
    found = runtime.conversations()
    if found is None:
        return
    for agent in store.agents(slug):
        if agent.state in {"finished", "awaiting-decision"} or not agent.pane_id:
            continue
        current = found.get(agent.pane_id, "")
        if current != agent.conversation_id:
            store.put_agent(slug, replace(agent, conversation_id=current))
            store.names.note(agent.name, session_id=current)


def _session_models(slug, store):
    for agent in store.agents(slug):
        reported = session_model.apply(agent, session_model.get(store.redis, slug, agent.name))
        if reported != agent:
            store.put_agent(slug, reported)


def _reopen(slug, ledger, rows, task_id):
    live = ledger.update_task(slug, task_id, {"state": "open", "claimed_by": ""}, if_state=ACTIVE)
    rows[task_id].update(live)
    return live["state"] == "open"


def _claimable(slug, store, rows, doc, lane):
    awaiting = {a.task for a in store.agents(slug) if a.state == "awaiting-decision"}
    held = [t.get("territory") or [] for t in rows.values() if t.get("state") in ACTIVE]
    clear, overlapping = [], []
    for t in sorted(rows.values(), key=claim_order.key(rows)):
        if (
            t.get("lane") == lane
            and t.get("state") == "open"
            and t["id"] not in awaiting
            and not t.get("out_of_scope")
            and not t.get("merged_into")
            and store.claimant(slug, t["id"]) is None
            and phase_state.admits(t, doc)
            and _unblocked(t, rows)
        ):
            mine = t.get("territory") or []
            if any(_overlaps(mine, other) for other in held):
                overlapping.append(t)
                continue
            clear.append(t)
            held.append(mine)
    return clear + overlapping


def _launch_order(slug, store, tasks):
    return sorted(tasks, key=lambda task: (ledger_rank.order(task), bool(store.launch_failure(slug, task["id"]))))


def _unblocked(task, rows):
    return not _parked(task, rows) and all(_stackable(rows.get(dep, {})) for dep in task.get("depends_on") or [])


def _parked(task, rows):
    return any(rows.get(dep, {}).get("state") != "done" for dep in task.get("parked_on") or [])


def _stackable(dep):
    return dep.get("state") == "done" or (dep.get("state") in ACTIVE and bool(dep.get("branch")))


def _stack_base(task, rows):
    deps = task.get("depends_on") or []
    return [{"task": dep, "branch": rows[dep]["branch"]} for dep in deps if rows[dep].get("state") != "done"]


def _refund_parked(slug, store, rows, task_id):
    if _parked(rows.get(task_id, {}), rows):
        store.refund_claim(slug, task_id)


def _overlaps(mine, theirs):
    return bool(_shared(mine, theirs))


def _shared(mine, theirs):
    pairs = [(a, b) for a in map(_area, mine) for b in map(_area, theirs)]
    return sorted({b if _nested(a, b) else a for a, b in pairs if _nested(a, b) or _nested(b, a)})


def _sharing(task, rows):
    mine = task.get("territory") or []
    running = [t for t in rows.values() if t.get("state") in ACTIVE and t["id"] != task["id"]]
    return [
        {"task": t["id"], "claimant": t.get("claimed_by") or "", "areas": areas}
        for t in running
        if (areas := _shared(mine, t.get("territory") or []))
    ]


def _area(entry):
    return entry.strip().removeprefix("./").rstrip("/")


def _nested(outer, inner):
    return inner == outer or inner.startswith(outer + "/")


def _held_for_master(slug, store, now_ms):
    waiting = store.redis.hgetall(MASTER_WAITING)
    others = sorted(s for s, at in waiting.items() if s != slug and now_ms - int(at) < MASTER_WAIT_MS)
    return [f"holding spawns: swarm {s} waits on a session slot for its master" for s in others[:1]]


def _record_spawn_failure(slug, store, record, error):
    transfers.failed(store, slug, record)
    if not isinstance(error, SpawnError) or error.status != "unavailable":
        store.note_launch_failure(slug, record.task, str(error))
    store.record_launch(slug, record, "failed", str(error))


def _spawn(slug, config, store, ledger, runtime, rows, doc, now_ms):
    agents, actions = store.agents(slug), []
    taken = {a.seat for a in agents}
    held = _held_for_master(slug, store, now_ms)
    for lane, task in _spawn_order(slug, config, store, agents, rows, doc):
        if held:
            return actions + held
        if not runtime.has_capacity(config):
            return actions + ["every agent is at its session cap, waiting"]
        if blocked := _lives_spent(slug, store, ledger, rows, task):
            actions.append(blocked)
            continue
        name = store.next_name(slug, lane, now_ms)
        if not store.claim(slug, task["id"], name, LEASE_MS):
            continue
        handoff = store.handoff(slug, task["id"])
        if handoff:
            task["handoff"] = handoff
        elif lives := store.earlier_lives(slug, task["id"]):
            task["reclaim"] = reclaim(config.repo, lives, task.get("branch") or "")
            store.put_reclaim(slug, name, task["reclaim"])
        task["stack_base"] = _stack_base(task, rows)
        task["overlaps"] = _sharing(task, rows)
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
                "workspace": str(ledger_workspace.scaffold(slug, task, doc)),
            }
            kind = config.lanes.get(lane, {}).get("kind", "")
            if kind not in ("", "auto") and not task.get("kind"):
                fields["kind"] = kind
            live = ledger.update_task(slug, task["id"], fields, if_state=("open",))
            task.update(live)
            if live["claimed_by"] != name:
                store.release(slug, task["id"], name)
                store.drop_agent(slug, name)
                actions.append(f"task {task['id']} is {live['state']} on the ledger, not claimed")
                continue
            task.update(fields)
            store.record_launch(slug, record, "pending")
            store.seats.occupy(seat, name, now_ms)
            task["transfer"] = transfers.attach(store, slug, record)
            placed = runtime.spawn(config, lane, name, primed(store, slug, seat, task))
        except Exception as exc:
            _record_spawn_failure(slug, store, record, exc)
            actions.append(f"spawn failed for {task['id']}{_drop(slug, store, ledger, rows, record)}: {exc}")
            if isinstance(exc, ProfileUnresolved):
                actions.append(_unresolved(slug, ledger, rows, task["id"], str(exc)))
            continue
        store.count_claim(slug, task["id"])
        store.record_launch(slug, record, "started")
        store.put_agent(slug, placed_record(record, placed))
        launch_check.begin(store, slug, record, now_ms)
        store.count_spawn(slug, placed.harness)
        store.clear_handoff(slug, task["id"])
        store.redis.hdel(store.key(slug, "launch-assignments"), task["id"])
        actions.append(f"spawned {name} for {task['id']}")
    return actions


def _spawn_order(slug, config, store, agents, rows, doc):
    from scripts.swarm import capacity

    decision = capacity.read(store, slug)
    caps = decision.get("effective", {"eng": config.max_eng, "ci": config.max_ci, "plan": config.max_plan})
    queue = []
    for lane, cap in caps.items():
        busy = sum(1 for a in agents if a.lane == lane and not _ended(a, rows))
        ready = _launch_order(slug, store, _claimable(slug, store, rows, doc, lane))
        if "tasks" in decision:
            ready = [task for task in ready if task["id"] in decision["tasks"]]
        ready = ready[: max(cap - busy, 0)]
        queue += [(busy + rank, lane, task) for rank, task in enumerate(ready)]
    return [(lane, task) for _, lane, task in sorted(queue, key=lambda entry: entry[0])]


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
    failure = store.launch_failure(slug, task["id"]) or "none"
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
    envelope = store.handoff_envelope(slug, task["id"])
    task = {**task, "seat": seat, "culture": store.culture.get(slug), "handoff_envelope": envelope, **memory}
    saved = store.redis.hget(store.key(slug, "launch-assignments"), task["id"])
    if saved:
        task["launch_assignment"] = json.loads(saved)
    return priming_trace.withhold(slug, task)


def _free_seat(slug, lane, taken, preferred):
    if preferred and preferred not in taken:
        return preferred
    return next(seat for k in count(1) if (seat := seat_address(slug, f"{lane}-{k}")) not in taken)


def placed_record(record, placed):
    return replace(
        record,
        pane_id=placed.pane_id,
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
        state="working",
    )


def _recover_master(slug, config, store, runtime, now_ms):
    agents = [a for a in store.agents(slug) if a.lane == MASTER]
    live = runtime.live_names()
    if any(a.state != "finished" and a.name in live for a in agents):
        return []
    finished = {a.name for a in agents if a.state == "finished"}
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
    record = AgentRecord(name, MASTER, MASTER, started_at=now_ms, seat=seat)
    store.put_agent(slug, placed_record(record, runtime.recover(name)))
    for agent in agents:
        if agent.name not in live and agent.state != "finished":
            store.drop_agent(slug, agent.name)
    if occupant != name:
        store.seats.occupy(seat, name, now_ms)
    return [f"adopted live master {name}"]


def _master(slug, config, store, runtime, now_ms):
    store.redis.hdel(MASTER_WAITING, slug)
    agents = store.agents(slug)
    masters = [a for a in agents if a.lane == MASTER]
    if config.state == "stopping":
        if any(a.lane != MASTER for a in agents):
            return []
        return [_retire_master(slug, store, runtime, m, now_ms) for m in masters]
    pending = master_start.read(store, slug)
    if any(m.state != "finished" for m in masters) or pending.get("name") or pending.get("alerted"):
        return []
    if any(m.name in runtime.live_names() for m in masters):
        return ["the old master is still running, waiting for it to end before starting the next"]
    if not runtime.has_capacity(config):
        store.redis.hset(MASTER_WAITING, slug, now_ms)
        return ["no session slot for the master, waiting"]
    name = store.next_name(slug, MASTER, now_ms)
    record = AgentRecord(name, MASTER, MASTER, started_at=now_ms, state="starting", seat=seat_address(slug, MASTER))
    store.put_agent(slug, record)
    try:
        store.seats.occupy(record.seat, name, now_ms)
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
        master_start.begin(store, slug, name, task, now_ms)
        affinity.handed_off(store, slug)
        placed = runtime.spawn(config, MASTER, name, task)
    except Exception as exc:
        transfers.failed(store, slug, record)
        store.drop_agent(slug, name)
        affinity.failed(store, slug, str(exc))
        failed = master_start.read(store, slug)
        if failed.get("attempt") == 1:
            master_start.save(store, slug, {**failed, "name": "", "retry": True})
        return [f"master spawn failed: {exc}"]
    affinity.placed(store, slug, placed.harness)
    record = placed_record(record, placed)
    reported = runtime.reported(record)
    store.put_agent(slug, replace(record, state="working" if reported else "starting"))
    launch_check.begin(store, slug, record, now_ms)
    if reported:
        store.redis.delete(store.key(slug, "master-start"))
        store.clear_handoff(slug, MASTER)
    store.redis.hdel(store.key(slug, "launch-assignments"), MASTER)
    return [f"spawned master {name}"]


def _retire_master(slug, store, runtime, master, now_ms):
    live = master.name in runtime.live_names()
    if held := master_retire.hold(store, slug, master, "the swarm is stopping", live, now_ms):
        return held
    if not runtime.retire(master, homes=reaper.scratch_homes(slug, master.task)):
        return f"could not retire {master.name}, retrying next tick"
    store.drop_agent(slug, master.name)
    return f"retired {master.name}"


def _settle(slug, config, store, ledger, rows, doc):
    agents = store.agents(slug)
    if config.state == "stopping":
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
    ledger.notify(slug, "The swarm has no task left to start" + waiting)
    return ["drained"]
