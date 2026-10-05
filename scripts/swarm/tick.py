"""One reconcile pass over a swarm: retire finished agents, free dead or stalled agents' tasks, spawn up to the caps.

Scaling up is immediate; scaling down happens only as agents finish, so a lowered cap never kills work.
Each swarm keeps at most one master: an agent the operator talks to, which works no task.
"""

from dataclasses import dataclass, replace
from itertools import count
from typing import Protocol

from scripts.doctor import priming
from scripts.inbox import exits
from scripts.inbox.seats import seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm import control_notifications, lifetime
from scripts.swarm import idle as idle_state
from scripts.swarm.naming import parse
from scripts.swarm.store import MASTER, AgentRecord, SwarmConfig
from scripts.swarm_ledger import ledger_workspace

LEASE_MS = 10 * 60 * 1000
STARTUP_GRACE_MS = 6 * 60 * 1000
IDLE_NUDGE_TICKS = 3
IDLE_KILL_TICKS = 10
LANES = ("eng", "ci")
ACTIVE = ("claimed", "pr")
NUDGE = (
    "Swarm check: you are idle and your task is still open. If you are waiting on checks or a deploy, declare it "
    "with agentihooks swarm {slug} wait <minutes> --reason <what> and keep waiting. Otherwise finish it with agentihooks swarm {slug} done "
    "and the proof your task's kind needs (--pr <url> for code), or agentihooks swarm {slug} block with the reason."
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


class Ledger(Protocol):
    def tasks(self, slug: str) -> list[dict]: ...
    def update_task(self, slug: str, task_id: str, fields: dict, by: str = "swarm") -> None: ...
    def notify(self, slug: str, text: str) -> None: ...
    def closed(self, slug: str) -> bool: ...


class Runtime(Protocol):
    def has_capacity(self) -> bool: ...
    def spawn(self, config, lane: str, name: str, task: dict, spawns: dict | None = None) -> Placed: ...
    def live_names(self) -> set[str]: ...
    def recover(self, name: str) -> Placed: ...
    def retire(self, agent: AgentRecord, live: bool) -> bool: ...
    def status(self, agent: AgentRecord) -> str: ...
    def nudge(self, agent: AgentRecord, text: str) -> None: ...
    def name_pane(self, agent: AgentRecord) -> bool: ...
    def conversations(self) -> dict[str, str] | None: ...
    def resume(self, config, agent: AgentRecord, text: str) -> Placed: ...
    def close_space(self, config: SwarmConfig) -> bool: ...


def tick(slug, store, ledger, runtime, now_ms):
    config = store.ensure_code(slug)
    actions = _recover_master(slug, config, store, runtime, now_ms)
    rows = {t["id"]: t for t in ledger.tasks(slug)}
    exits.sweep(InboxStore(store.redis), slug, store, rows)
    actions += _reap(slug, store, ledger, runtime, rows, now_ms)
    actions += lifetime.retire_idle_master(slug, store, ledger, runtime, rows, now_ms)
    if config.state == "stopped":
        retired = store.redis.get(store.key(slug, "master-retired-tasks")) is not None
        if not _woken(slug, config, store, ledger) and (not retired or lifetime.sleeping(slug, store, rows)):
            return actions + _close_space(slug, config, store, runtime)
        config = store.update(slug, state="paused")
        actions.append("the operator wrote on the ledger, paused to start the master")
        actions += _recover_master(slug, config, store, runtime, now_ms)
    sleeping = lifetime.sleeping(slug, store, rows)
    if not sleeping and config.state == "drained" and any(_claimable(slug, store, rows, lane) for lane in LANES):
        config = store.update(slug, state="running")
        actions.append("new tasks, running again")
    actions += _orphans(slug, store, ledger, rows)
    if not sleeping:
        actions += _master(slug, config, store, runtime, now_ms)
        if config.state == "running":
            actions += _spawn(slug, config, store, ledger, runtime, rows, now_ms)
    _conversations(slug, store, runtime)
    return actions + _settle(slug, config, store, ledger, rows) + _close_space(slug, config, store, runtime)


def _close_space(slug, config, store, runtime):
    if not store.agents(slug):
        runtime.close_space(config)
    return []


def _woken(slug, config, store, ledger):
    inbox = InboxStore(store.redis)
    waiting = inbox.pending_items(seat_address(slug, MASTER))
    if waiting and config.template == priming.TEMPLATE and ledger.closed(slug):
        priming.cancel_master_items(inbox, slug)
        return False
    return any(not (item.fyi and item.ref.startswith(control_notifications.CONTROL_REF)) for item in waiting)


def _drop(slug, store, ledger, rows, agent):
    store.release(slug, agent.task, agent.name)
    store.drop_agent(slug, agent.name)
    reopen = rows.get(agent.task, {}).get("state") in ACTIVE and rows[agent.task].get("claimed_by") == agent.name
    if reopen:
        _reopen(slug, ledger, rows, agent.task)
    exits.settle(InboxStore(store.redis), agent.name, agent.seat if reopen else "", "stopped")
    return f", task {agent.task} reopened" if reopen else ""


def _reap(slug, store, ledger, runtime, rows, now_ms):
    live, actions = runtime.live_names(), []
    for agent in store.agents(slug):
        task = rows.get(agent.task, {})
        ended = agent.lane != MASTER and (task.get("done") or task.get("state") in {"done", "blocked", "handoff"})
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
        elif agent.name in live:
            store.refresh(slug, agent.task, agent.name, LEASE_MS)
            actions += _watch_idle(slug, store, ledger, runtime, rows, agent, now_ms)
        elif now_ms - agent.started_at > STARTUP_GRACE_MS:
            runtime.retire(agent, False)
            actions.append(f"lost {agent.name}" + _drop(slug, store, ledger, rows, agent))
    return actions


def agent_status(agent):
    if agent.state == "finished":
        return "finished"
    if agent.idle_ticks >= IDLE_NUDGE_TICKS:
        return "stalled"
    return "idle" if agent.idle_ticks else "working"


def _watch_idle(slug, store, ledger, runtime, rows, agent, now_ms):
    state = idle_state.of(store, runtime, slug, agent, now_ms)
    if state == idle_state.WAITING:
        return []
    if state == idle_state.WORKING:
        if agent.idle_ticks:
            store.put_agent(slug, replace(agent, idle_ticks=0))
        return []
    idle = replace(agent, idle_ticks=agent.idle_ticks + 1)
    store.put_agent(slug, idle)
    if idle.idle_ticks == IDLE_NUDGE_TICKS:
        runtime.nudge(idle, NUDGE.format(slug=slug))
        return [f"nudged {agent.name}"]
    if idle.idle_ticks >= IDLE_KILL_TICKS and runtime.retire(idle, True):
        return [f"stalled {agent.name}" + _drop(slug, store, ledger, rows, idle)]
    return []


def _orphans(slug, store, ledger, rows):
    known = {a.name for a in store.agents(slug)}
    actions = []
    for task_id, row in rows.items():
        if row.get("state") in ACTIVE and row.get("claimed_by") not in known and store.claimant(slug, task_id) is None:
            _reopen(slug, ledger, rows, task_id)
            actions.append(f"task {task_id} had no agent, reopened")
    return actions


def _conversations(slug, store, runtime):
    found = runtime.conversations()
    if found is None:
        return
    for agent in store.agents(slug):
        if agent.state == "finished" or not agent.pane_id:
            continue
        current = found.get(agent.pane_id, "")
        if current != agent.conversation_id:
            store.put_agent(slug, replace(agent, conversation_id=current))
            store.names.note(agent.name, session_id=current)


def _reopen(slug, ledger, rows, task_id):
    ledger.update_task(slug, task_id, {"state": "open", "claimed_by": ""})
    rows[task_id].update(state="open", claimed_by="")


def _claimable(slug, store, rows, lane):
    held = [t.get("territory") or [] for t in rows.values() if t.get("state") in ACTIVE]
    picked = []
    for t in rows.values():
        if (
            t.get("lane") == lane
            and t.get("state") == "open"
            and not t.get("out_of_scope")
            and store.claimant(slug, t["id"]) is None
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


def _spawn(slug, config, store, ledger, runtime, rows, now_ms):
    agents, actions = store.agents(slug), []
    taken = {a.seat for a in agents}
    for lane, cap in (("eng", config.max_eng), ("ci", config.max_ci)):
        busy = sum(1 for a in agents if a.lane == lane)
        for task in _claimable(slug, store, rows, lane)[: max(cap - busy, 0)]:
            if not runtime.has_capacity():
                return actions + ["every agent is at its session cap, waiting"]
            name = store.next_name(slug, lane, now_ms)
            if not store.claim(slug, task["id"], name, LEASE_MS):
                continue
            handoff = store.handoff(slug, task["id"])
            if handoff:
                task["handoff"] = handoff
            seat = _free_seat(slug, lane, taken, store.handoff_seat(slug, task["id"]))
            taken.add(seat)
            record = AgentRecord(name, lane, task["id"], started_at=now_ms, state="starting", seat=seat)
            store.put_agent(slug, record)
            try:
                state = "pr" if task.get("pr_url") else "claimed"
                fields = {"state": state, "claimed_by": name, "workspace": str(ledger_workspace.scaffold(slug, task))}
                kind = config.lanes.get(lane, {}).get("kind", "")
                if kind not in ("", "auto") and not task.get("kind"):
                    fields["kind"] = kind
                ledger.update_task(slug, task["id"], fields)
                task.update(fields)
                store.seats.occupy(seat, name, now_ms)
                placed = runtime.spawn(config, lane, name, primed(store, slug, seat, task), spawns=store.spawns(slug))
            except Exception as exc:
                actions.append(f"spawn failed for {task['id']}{_drop(slug, store, ledger, rows, record)}: {exc}")
                return actions
            store.put_agent(slug, _placed(record, placed))
            store.count_spawn(slug, placed.harness)
            store.clear_handoff(slug, task["id"])
            actions.append(f"spawned {name} for {task['id']}")
    return actions


def primed(store, slug, seat, task):
    memory = {"recaps": store.memory.recaps(seat), "learned": store.memory.learned(seat)}
    return {**task, "seat": seat, "culture": store.culture.get(slug), **memory}


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
        state="working",
    )


def _recover_master(slug, config, store, runtime, now_ms):
    if config.state == "stopped":
        return []
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
    if any(m.state != "finished" for m in masters):
        return []
    if not runtime.has_capacity():
        return ["no session slot for the master, waiting"]
    name = store.next_name(slug, MASTER, now_ms)
    record = AgentRecord(name, MASTER, MASTER, started_at=now_ms, state="starting", seat=seat_address(slug, MASTER))
    store.put_agent(slug, record)
    try:
        store.seats.occupy(record.seat, name, now_ms)
        placed = runtime.spawn(
            config,
            MASTER,
            name,
            primed(
                store,
                slug,
                record.seat,
                {"id": MASTER, "handoff": store.handoff(slug, MASTER), "peer": store.peer(slug)},
            ),
        )
    except Exception as exc:
        store.drop_agent(slug, name)
        return [f"master spawn failed: {exc}"]
    store.put_agent(slug, _placed(record, placed))
    store.clear_handoff(slug, MASTER)
    return [f"spawned master {name}"]


def _retire_master(slug, store, runtime, master):
    if not runtime.retire(master, master.name in runtime.live_names()):
        return f"could not retire {master.name}, retrying next tick"
    store.drop_agent(slug, master.name)
    return f"retired {master.name}"


def _settle(slug, config, store, ledger, rows):
    agents = store.agents(slug)
    if config.state == "stopping":
        if agents:
            return []
        store.update(slug, state="stopped")
        return ["stopped"]
    if any(a.lane != MASTER for a in agents):
        return []
    if config.state != "running" or any(_claimable(slug, store, rows, lane) for lane in LANES):
        return []
    store.update(slug, state="drained")
    blocked = sum(1 for t in rows.values() if t.get("state") == "blocked" and not t.get("out_of_scope"))
    waiting = {0: "", 1: ", one blocked task waits for you"}.get(blocked, f", {blocked} blocked tasks wait for you")
    ledger.notify(slug, "The swarm has no task left to start" + waiting)
    return ["drained"]
