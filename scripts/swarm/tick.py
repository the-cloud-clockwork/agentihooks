"""One reconcile pass over a swarm: retire finished agents, free dead or stalled agents' tasks, spawn up to the caps.

Scaling up is immediate; scaling down happens only as agents finish, so a lowered cap never kills work.
Every swarm that is not stopped keeps one master: an agent the operator talks to, which works no task.
"""

from dataclasses import dataclass, replace
from typing import Protocol

from scripts.swarm.store import MASTER, AgentRecord

LEASE_MS = 10 * 60 * 1000
STARTUP_GRACE_MS = 6 * 60 * 1000
IDLE_NUDGE_TICKS = 3
IDLE_KILL_TICKS = 10
LANES = ("eng", "ci")
ACTIVE = ("claimed", "pr")
NUDGE = (
    "Swarm check: you are idle and your task is still open. If you are waiting on checks, say so with "
    "agentihooks swarm {slug} say and keep waiting. Otherwise finish it with agentihooks swarm {slug} done "
    "--pr <url>, or agentihooks swarm {slug} block with the reason."
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


class Ledger(Protocol):
    def tasks(self, slug: str) -> list[dict]: ...
    def update_task(self, slug: str, task_id: str, fields: dict, by: str = "swarm") -> None: ...
    def notify(self, slug: str, text: str) -> None: ...


class Runtime(Protocol):
    def has_capacity(self) -> bool: ...
    def spawn(self, config, lane: str, name: str, task: dict) -> Placed: ...
    def live_names(self) -> set[str]: ...
    def retire(self, agent: AgentRecord, live: bool) -> bool: ...
    def status(self, agent: AgentRecord) -> str: ...
    def nudge(self, agent: AgentRecord, text: str) -> None: ...


def tick(slug, store, ledger, runtime, now_ms):
    config = store.config(slug)
    if config.state == "stopped":
        return []
    rows = {t["id"]: t for t in ledger.tasks(slug)}
    actions = []
    if config.state == "drained" and any(_claimable(slug, store, rows, lane) for lane in LANES):
        config = store.update(slug, state="running")
        actions.append("new tasks, running again")
    actions += _reap(slug, store, ledger, runtime, rows, now_ms)
    actions += _orphans(slug, store, ledger, rows)
    actions += _master(slug, config, store, runtime, now_ms)
    if config.state == "running":
        actions += _spawn(slug, config, store, ledger, runtime, rows, now_ms)
    return actions + _settle(slug, config, store, ledger, rows)


def _drop(slug, store, ledger, rows, agent):
    store.release(slug, agent.task, agent.name)
    store.drop_agent(slug, agent.name)
    if rows.get(agent.task, {}).get("state") in ACTIVE and rows[agent.task].get("claimed_by") == agent.name:
        _reopen(slug, ledger, rows, agent.task)
        return f", task {agent.task} reopened"
    return ""


def _reap(slug, store, ledger, runtime, rows, now_ms):
    live, actions = runtime.live_names(), []
    for agent in store.agents(slug):
        if agent.state == "finished":
            if runtime.retire(agent, agent.name in live):
                store.release(slug, agent.task, agent.name)
                store.drop_agent(slug, agent.name)
                if store.handoff(slug, agent.task) and rows.get(agent.task, {}).get("state") in ACTIVE:
                    _reopen(slug, ledger, rows, agent.task)
                actions.append(f"retired {agent.name}")
            else:
                actions.append(f"could not retire {agent.name}, retrying next tick")
        elif agent.name in live and agent.lane == MASTER:
            continue
        elif agent.name in live:
            store.refresh(slug, agent.task, agent.name, LEASE_MS)
            actions += _watch_idle(slug, store, ledger, runtime, rows, agent)
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


def _watch_idle(slug, store, ledger, runtime, rows, agent):
    if runtime.status(agent) not in ("idle", "done"):
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
    for lane, cap in (("eng", config.max_eng), ("ci", config.max_ci)):
        busy = sum(1 for a in agents if a.lane == lane)
        for task in _claimable(slug, store, rows, lane)[: max(cap - busy, 0)]:
            if not runtime.has_capacity():
                return actions + ["every agent is at its session cap, waiting"]
            name = store.next_name(slug, lane)
            if not store.claim(slug, task["id"], name, LEASE_MS):
                continue
            handoff = store.handoff(slug, task["id"])
            if handoff:
                task["handoff"] = handoff
            record = AgentRecord(name, lane, task["id"], started_at=now_ms, state="starting")
            store.put_agent(slug, record)
            try:
                state = "pr" if task.get("pr_url") else "claimed"
                ledger.update_task(slug, task["id"], {"state": state, "claimed_by": name})
                task.update(state=state, claimed_by=name)
                placed = runtime.spawn(config, lane, name, task)
            except Exception as exc:
                actions.append(f"spawn failed for {task['id']}{_drop(slug, store, ledger, rows, record)}: {exc}")
                return actions
            store.put_agent(slug, _placed(record, placed))
            store.clear_handoff(slug, task["id"])
            actions.append(f"spawned {name} for {task['id']}")
    return actions


def _placed(record, placed):
    return replace(
        record,
        pane_id=placed.pane_id,
        harness=placed.harness,
        account=placed.account,
        model=placed.model,
        effort=placed.effort,
        state="working",
    )


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
    name = store.next_name(slug, MASTER)
    record = AgentRecord(name, MASTER, MASTER, started_at=now_ms, state="starting")
    store.put_agent(slug, record)
    try:
        placed = runtime.spawn(config, MASTER, name, {"id": MASTER, "handoff": store.handoff(slug, MASTER)})
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
