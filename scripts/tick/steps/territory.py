# pyright: reportUnusedFunction=false
"""Task territory overlap, parked and stacked dependencies, and the settle and close of a swarm.

Moved code reaches swarm tick module names through ``tick`` so patched tick seams apply.
"""

from importlib import import_module
from typing import Any

from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

GHOST = "the swarm is stopping and its runtime never answered"


def _parked(task: Any, rows: Any) -> Any:
    return any(rows.get(dep, {}).get("state") != "done" for dep in task.get("parked_on") or [])  # pyright: ignore[reportUnknownVariableType]


def _stack_base(task: Any, rows: Any, repo: Any, origin: Any = None) -> Any:
    deps = [dep for dep in task.get("depends_on") or [] if rows[dep].get("state") != "done"]  # pyright: ignore[reportUnknownVariableType]
    return [tick._stacked_dep(dep, rows[dep], repo, origin or tick.stack.origin_key) for dep in deps]  # pyright: ignore[reportUnknownVariableType]


def _stacked_dep(dep: Any, row: Any, repo: Any, origin: Any) -> Any:
    base = {"task": dep, "branch": row["branch"]}
    elsewhere = row.get("branch_repo") and tick.stack.repo_key(row["branch_repo"]) != origin(repo)
    return {**base, "repo": row["branch_repo"]} if elsewhere else base


def _refund_parked(slug: Any, store: Any, rows: Any, agent: Any) -> Any:
    reason = (store.handoff_envelope(slug, agent.task) or {}).get("reason")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    preempted = tick.preempt.take_victim(store, slug, agent.name)
    if tick._parked(rows.get(agent.task, {}), rows) or reason == "quota" or (reason == "operator" and preempted):
        store.refund_held_claim(slug, agent.task, agent.name, store.claim_generation(slug, agent.task))


def _overlaps(mine: Any, theirs: Any) -> Any:
    return bool(tick._shared(mine, theirs))


def _shared(mine: Any, theirs: Any) -> Any:
    pairs = [(a, b) for a in map(tick._area, mine) for b in map(tick._area, theirs)]
    return sorted({b if tick._nested(a, b) else a for a, b in pairs if tick._nested(a, b) or tick._nested(b, a)})


def _sharing(task: Any, rows: Any, agents: Any) -> Any:
    mine = task.get("territory") or []  # pyright: ignore[reportUnknownVariableType]
    owners = {a.task: a.name for a in agents if not tick._ended(a, rows)}
    running = [t for t in rows.values() if t.get("state") in tick.ACTIVE and t["id"] != task["id"]]
    return [
        {
            "task": t["id"],
            "claimant": owners.get(t["id"], ""),
            "areas": areas,
            **({"checks": t["state"] == "pr"} if t["id"] not in owners else {}),
        }
        for t in running
        if (areas := tick._shared(mine, t.get("territory") or []))
    ]


def _area(entry: Any) -> Any:
    return entry.strip().removeprefix("./").rstrip("/")


def _nested(outer: Any, inner: Any) -> Any:
    return inner == outer or inner.startswith(outer + "/")


def _settle(
    slug: str, config: SwarmConfig, store: RedisStore, ledger: Any, rows: dict[str, dict[str, Any]], doc: Any
) -> list[str]:
    agents = store.agents(slug)  # pyright: ignore[reportUnknownMemberType]
    if config.state == "stopping":
        if not any(a.lane == tick.MASTER and a.state != "finished" for a in agents):
            tick._end_awaiting_seats(slug, store, ledger, rows, agents)
            agents = [a for a in agents if a.state != tick.resume.AWAITING]
        if any(a.state != tick.SUSPECT for a in agents):
            return []
        retired = [tick._drop_remote(slug, store, ledger, rows, a, GHOST) for a in agents]
        store.update(slug, state="stopped")  # pyright: ignore[reportUnknownMemberType]
        return [*retired, "stopped"]
    if any(a.lane != tick.MASTER for a in agents):
        return []
    if config.state != "running" or any(tick._claimable(slug, store, rows, doc, lane) for lane in tick.LANES):
        return []
    store.update(slug, state="drained")  # pyright: ignore[reportUnknownMemberType]
    blocked = sum(1 for t in rows.values() if t.get("state") == "blocked" and not t.get("out_of_scope"))
    waiting = {0: "", 1: ", one blocked task waits for you"}.get(blocked, f", {blocked} blocked tasks wait for you")
    ledger.notify(slug, tick.freeze.notice(doc, rows.values(), tick.freeze.fix_phase(config)) + waiting)
    return ["drained"]


def _end_awaiting_seats(
    slug: str, store: RedisStore, ledger: Any, rows: dict[str, dict[str, Any]], agents: list[AgentRecord]
) -> None:
    for agent in agents:
        if agent.state == tick.resume.AWAITING:
            tick._drop(slug, store, ledger, rows, agent)


def _close_space(slug: Any, config: Any, store: Any, runtime: Any) -> Any:
    if not store.agents(slug):
        runtime.close_space(config)
    return []


def _bin_closed(slug: Any, store: Any, ledger: Any, doc: Any) -> Any:
    if doc.get("closed_at") and not store.agents(slug):
        tick.exits.close_swarm(tick.InboxStore(store.redis), slug)
        if ledger.bin_closed(slug, doc["closed_at"]):
            return ["the closed ledger moved to the bin"]
    return []


tick: Any = import_module("scripts.swarm.tick")
