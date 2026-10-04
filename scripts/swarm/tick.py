"""One reconcile pass over a swarm: retire finished agents, free dead agents' tasks, spawn up to the caps.

Scaling up is immediate; scaling down happens only as agents finish, so a lowered cap never kills work.
"""

from dataclasses import dataclass
from typing import Protocol

from scripts.swarm.store import AgentRecord

LEASE_MS = 10 * 60 * 1000
STARTUP_GRACE_MS = 3 * 60 * 1000
LANES = ("eng", "ci")


class SpawnError(RuntimeError):
    pass


@dataclass(frozen=True)
class Placed:
    pane_id: str
    harness: str
    account: str = ""


class Ledger(Protocol):
    def tasks(self, slug: str) -> list[dict]: ...
    def update_task(self, slug: str, task_id: str, fields: dict, by: str = "swarm") -> None: ...
    def notify(self, slug: str, text: str) -> None: ...


class Runtime(Protocol):
    def spawn(self, config, lane: str, name: str, task: dict) -> Placed: ...
    def live_names(self) -> set[str]: ...
    def terminate(self, name: str) -> None: ...


def tick(slug, store, ledger, runtime, now_ms):
    config = store.config(slug)
    if config.state in ("stopped", "drained"):
        return []
    rows = {t["id"]: t for t in ledger.tasks(slug)}
    actions = _reap(slug, store, ledger, runtime, rows, now_ms)
    if config.state == "running":
        actions += _spawn(slug, config, store, ledger, runtime, rows, now_ms)
    actions += _settle(slug, config, store, ledger, rows)
    return actions


def _reap(slug, store, ledger, runtime, rows, now_ms):
    live, actions = runtime.live_names(), []
    for agent in store.agents(slug):
        if agent.state == "finished":
            if agent.name in live:
                runtime.terminate(agent.name)
            store.release(slug, agent.task, agent.name)
            store.drop_agent(slug, agent.name)
            actions.append(f"retired {agent.name}")
        elif agent.name in live:
            store.refresh(slug, agent.task, agent.name, LEASE_MS)
        elif now_ms - agent.started_at > STARTUP_GRACE_MS:
            store.release(slug, agent.task, agent.name)
            store.drop_agent(slug, agent.name)
            if rows.get(agent.task, {}).get("state") == "claimed":
                _reopen(slug, ledger, rows, agent.task)
            actions.append(f"lost {agent.name}, task {agent.task} reopened")
    return actions


def _reopen(slug, ledger, rows, task_id):
    ledger.update_task(slug, task_id, {"state": "open", "claimed_by": ""})
    rows[task_id].update(state="open", claimed_by="")


def _claimable(slug, store, rows, lane):
    return [
        t
        for t in rows.values()
        if t.get("lane") == lane
        and t.get("state") == "open"
        and not t.get("out_of_scope")
        and store.claimant(slug, t["id"]) is None
    ]


def _spawn(slug, config, store, ledger, runtime, rows, now_ms):
    agents, actions = store.agents(slug), []
    for lane, cap in (("eng", config.max_eng), ("ci", config.max_ci)):
        busy = sum(1 for a in agents if a.lane == lane)
        for task in _claimable(slug, store, rows, lane)[: max(cap - busy, 0)]:
            name = store.next_name(slug, lane)
            if not store.claim(slug, task["id"], name, LEASE_MS):
                continue
            ledger.update_task(slug, task["id"], {"state": "claimed", "claimed_by": name})
            task.update(state="claimed", claimed_by=name)
            try:
                placed = runtime.spawn(config, lane, name, task)
            except SpawnError as exc:
                store.release(slug, task["id"], name)
                _reopen(slug, ledger, rows, task["id"])
                actions.append(f"spawn failed for {task['id']}: {exc}")
                return actions
            store.put_agent(
                slug, AgentRecord(name, lane, task["id"], placed.pane_id, placed.harness, placed.account, now_ms)
            )
            actions.append(f"spawned {name} for {task['id']}")
    return actions


def _settle(slug, config, store, ledger, rows):
    if store.agents(slug):
        return []
    if config.state == "stopping":
        store.update(slug, state="stopped")
        return ["stopped"]
    if config.state != "running" or any(_claimable(slug, store, rows, lane) for lane in LANES):
        return []
    store.update(slug, state="drained")
    blocked = [t["id"] for t in rows.values() if t.get("state") == "blocked" and not t.get("out_of_scope")]
    ledger.notify(slug, f"swarm {slug} drained" + (f", {len(blocked)} blocked task(s) wait for you" if blocked else ""))
    return ["drained"]
