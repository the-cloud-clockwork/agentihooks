"""One reconcile pass over a swarm: retire finished agents, free dead or stalled agents' tasks, spawn up to the caps.

Scaling up is immediate; scaling down happens only as agents finish, so a lowered cap never kills work.
Each swarm keeps at most one master: an agent the operator talks to, which works no task.
"""

import json
import os
from dataclasses import dataclass, field, replace
from itertools import count
from typing import Protocol

from scripts.doctor import priming
from scripts.gates import Who, modes
from scripts.gates import claims as claim_cap
from scripts.gates import log as gate_log
from scripts.handoff import transfers
from scripts.inbox import exits, wake
from scripts.inbox.seats import seat_address
from scripts.inbox.store import CLOSED, InboxStore
from scripts.swarm import (
    affinity,
    control_notifications,
    lifetime,
    live_binding,
    master_start,
    phase_state,
    session_model,
)
from scripts.swarm import idle as idle_state
from scripts.swarm.naming import parse
from scripts.swarm.pane import PaneObservation
from scripts.swarm.profile_choice import ProfileUnresolved
from scripts.swarm.store import MASTER, AgentRecord, SwarmConfig
from scripts.swarm_ledger import ledger_rank, ledger_workspace

LEASE_MS = 10 * 60 * 1000
STARTUP_GRACE_MS = 6 * 60 * 1000
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
    pass


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


class Ledger(Protocol):
    def state(self, slug: str) -> dict: ...
    def update_task(self, slug: str, task_id: str, fields: dict, by: str = ..., if_state: tuple = ...) -> dict: ...
    def comment(self, slug: str, task_id: str, text: str, by: str) -> None: ...
    def notify(self, slug: str, text: str) -> None: ...
    def closed(self, slug: str) -> bool: ...
    def bin_closed(self, slug: str, closed_at: int) -> bool: ...


class Runtime(Protocol):
    def has_capacity(self, config) -> bool: ...
    def spawn(self, config, lane: str, name: str, task: dict, spawns: dict | None = None) -> Placed: ...
    def live_names(self) -> set[str]: ...
    def reported(self, agent: AgentRecord) -> bool: ...
    def bindings(self, agents: list[AgentRecord]) -> dict: ...
    def pane_open(self, agent: AgentRecord) -> bool: ...
    def recover(self, name: str) -> Placed: ...
    def retire(self, agent: AgentRecord, live: bool) -> bool: ...
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
        actions = _recover_master(slug, config, store, runtime, now_ms)
    doc = ledger.state(slug)
    rows = {t["id"]: t for t in doc["tasks"]}
    exits.sweep(InboxStore(store.redis), slug, store, lambda: {t["id"]: t for t in ledger.state(slug)["tasks"]})
    actions += master_start.observe(slug, config, store, ledger, runtime, now_ms)
    actions += _verify(slug, store, ledger, runtime, rows, now_ms)
    actions += _reap(slug, store, ledger, runtime, rows, now_ms)
    actions += lifetime.retire_idle_master(slug, store, ledger, runtime, rows, now_ms)
    if config.state == "stopped":
        retired = store.redis.get(store.key(slug, "master-retired-tasks")) is not None
        if not _woken(slug, config, store, ledger) and (not retired or lifetime.sleeping(slug, store, rows)):
            return actions + _close_space(slug, config, store, runtime) + _bin_closed(slug, store, ledger, doc)
        config = store.update(slug, state="paused")
        actions.append("the operator wrote on the ledger, paused to start the master")
    sleeping = lifetime.sleeping(slug, store, rows)
    if not sleeping and config.state == "drained" and any(_claimable(slug, store, rows, doc, lane) for lane in LANES):
        config = store.update(slug, state="running")
        actions.append("new tasks, running again")
    actions += _orphans(slug, store, ledger, rows)
    if not sleeping:
        actions += _master_down(slug, config, store, ledger, runtime, now_ms)
        actions += _master(slug, config, store, runtime, now_ms)
        if config.state == "running":
            actions += _spawn(slug, config, store, ledger, runtime, rows, doc, now_ms)
    _conversations(slug, store, runtime)
    _session_models(slug, store)
    starting = {a.name for a in store.agents(slug) if a.lane == MASTER and a.state == "starting"}
    transfers.observe(store, slug, runtime.live_names() - starting)
    return actions + _settle(slug, config, store, ledger, rows, doc) + _close_space(slug, config, store, runtime)


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
    agents, actions = store.agents(slug), []
    facts = runtime.bindings(agents)
    for agent in agents:
        if agent.state == "awaiting-decision" or (agent.lane == MASTER and agent.state == "starting"):
            continue
        task = rows.get(agent.task, {})
        ended = agent.lane != MASTER and (task.get("done") or task.get("state") in {"done", "blocked", "handoff"})
        if agent.state == "finished" or ended:
            if runtime.pane_open(agent):
                live_binding.record(store, slug, agent, {"pane": "open"}, now_ms)
            continue
        if agent.name not in facts and agent.state != "retiring":
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
        if not runtime.retire(agent, agent.name in facts):
            store.put_agent(slug, replace(agent, state="retiring"))
            actions.append(f"could not retire {agent.name} after mismatched {fields}, retrying next tick")
            continue
        store.redis.hset(store.key(slug, "launch-assignments"), agent.task, json.dumps(saved))
        actions.append(f"retired {agent.name} after mismatched {fields}" + _drop(slug, store, ledger, rows, agent))
    return actions


def _reap(slug, store, ledger, runtime, rows, now_ms):
    live, actions = runtime.live_names(), []
    for agent in store.agents(slug):
        if agent.state == "awaiting-decision" or (agent.lane == MASTER and agent.state == "starting"):
            continue
        task = rows.get(agent.task, {})
        ended = agent.lane != MASTER and (task.get("done") or task.get("state") in {"done", "blocked", "handoff"})
        if agent.state == "retiring" and not ended:
            continue
        if agent.state == "finished" or ended:
            if runtime.retire(agent, agent.name in live):
                store.release(slug, agent.task, agent.name)
                store.drop_agent(slug, agent.name)
                goes_on = bool(store.handoff(slug, agent.task)) and rows.get(agent.task, {}).get("state") in ACTIVE
                if goes_on:
                    _reopen(slug, ledger, rows, agent.task)
                exits.settle(InboxStore(store.redis), agent.name, agent.seat if goes_on else "", "exited")
                actions.append(f"retired {agent.name}")
            else:
                actions.append(f"could not retire {agent.name}, retrying next tick")
        elif agent.name in live and agent.lane == MASTER:
            runtime.name_pane(agent)
            actions += _watch_idle(slug, store, ledger, runtime, rows, agent, now_ms)
        elif agent.name in live:
            store.refresh(slug, agent.task, agent.name, LEASE_MS)
            actions += _watch_idle(slug, store, ledger, runtime, rows, agent, now_ms)
        elif now_ms - agent.started_at > STARTUP_GRACE_MS:
            runtime.retire(agent, False)
            actions.append(f"lost {agent.name}" + _drop(slug, store, ledger, rows, agent))
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
    if idle.idle_ticks >= IDLE_KILL_TICKS and runtime.retire(idle, True):
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
    picked = []
    for t in sorted(rows.values(), key=ledger_rank.order):
        if (
            t.get("lane") == lane
            and t.get("state") == "open"
            and t["id"] not in awaiting
            and not t.get("out_of_scope")
            and store.claimant(slug, t["id"]) is None
            and phase_state.admits(t, doc)
            and _unblocked(t, rows, held)
        ):
            picked.append(t)
            held.append(t.get("territory") or [])
    return picked


def _unblocked(task, rows, held):
    if any(rows.get(dep, {}).get("state") != "done" for dep in task.get("depends_on") or []):
        return False
    return not any(_overlaps(task.get("territory") or [], other) for other in held)


def _overlaps(mine, theirs):
    return any(_nested(a, b) or _nested(b, a) for a in map(_area, mine) for b in map(_area, theirs))


def _area(entry):
    return entry.strip().removeprefix("./").rstrip("/")


def _nested(outer, inner):
    return inner == outer or inner.startswith(outer + "/")


def _spawn(slug, config, store, ledger, runtime, rows, doc, now_ms):
    agents, actions = store.agents(slug), []
    taken = {a.seat for a in agents}
    for lane, cap in (("eng", config.max_eng), ("ci", config.max_ci), ("plan", config.max_plan)):
        busy = sum(1 for a in agents if a.lane == lane)
        for task in _claimable(slug, store, rows, doc, lane)[: max(cap - busy, 0)]:
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
                store.seats.occupy(seat, name, now_ms)
                task["transfer"] = transfers.attach(store, slug, record)
                placed = runtime.spawn(
                    config, lane, name, primed(store, slug, seat, task), spawns=store.share_picks(slug, now_ms)
                )
            except Exception as exc:
                transfers.failed(store, slug, record)
                actions.append(f"spawn failed for {task['id']}{_drop(slug, store, ledger, rows, record)}: {exc}")
                if isinstance(exc, ProfileUnresolved):
                    actions.append(_unresolved(slug, ledger, rows, task["id"], str(exc)))
                return actions
            store.put_agent(slug, _placed(record, placed))
            store.count_spawn(slug, placed.harness)
            store.count_claim(slug, task["id"])
            store.clear_handoff(slug, task["id"])
            store.redis.hdel(store.key(slug, "launch-assignments"), task["id"])
            actions.append(f"spawned {name} for {task['id']}")
    return actions


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
    reason = claim_cap.refusal(lives, last, slug, task["id"])
    kind = "observe" if mode == "observe" else "deny"
    gate_log.append(slug, gate_log.Row.of(claim_cap.GATE.name, kind, Who(name="swarm", task=task["id"]), reason=reason))
    if kind == "observe":
        return ""
    live = ledger.update_task(slug, task["id"], {"state": "blocked"}, if_state=("open",))
    rows[task["id"]].update(live)
    if live["state"] != "blocked":
        return ""
    ledger.comment(slug, task["id"], reason, by="swarm")
    store.reset_claims(slug, task["id"])
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


def _placed(record, placed):
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
    store.put_agent(slug, _placed(record, runtime.recover(name)))
    for agent in agents:
        if agent.name not in live and agent.state != "finished":
            store.drop_agent(slug, agent.name)
    if occupant != name:
        store.seats.occupy(seat, name, now_ms)
    return [f"adopted live master {name}"]


def _master(slug, config, store, runtime, now_ms):
    agents = store.agents(slug)
    masters = [a for a in agents if a.lane == MASTER]
    if config.state == "stopping":
        if any(a.lane != MASTER for a in agents):
            return []
        return [_retire_master(slug, store, runtime, m) for m in masters]
    pending = master_start.read(store, slug)
    if any(m.state != "finished" for m in masters) or pending.get("name") or pending.get("alerted"):
        return []
    if any(m.name in runtime.live_names() for m in masters):
        return ["the old master is still running, waiting for it to end before starting the next"]
    if not runtime.has_capacity(config):
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
    record = _placed(record, placed)
    reported = runtime.reported(record)
    store.put_agent(slug, replace(record, state="working" if reported else "starting"))
    if reported:
        store.redis.delete(store.key(slug, "master-start"))
        store.clear_handoff(slug, MASTER)
    store.redis.hdel(store.key(slug, "launch-assignments"), MASTER)
    return [f"spawned master {name}"]


def _retire_master(slug, store, runtime, master):
    if not runtime.retire(master, master.name in runtime.live_names()):
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
