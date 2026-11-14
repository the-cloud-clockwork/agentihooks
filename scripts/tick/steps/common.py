"""Helpers the tick steps and the swarm tick module share: skip wrappers, claim drops, rebinding and task ends."""

# pyright: reportUnusedFunction=false

import json
import sys
from collections.abc import Callable
from dataclasses import replace
from importlib import import_module
from typing import Any

from scripts.swarm import claim_lost

DROP_REFUSED = ", the store refused its drop, retrying next pass"


def skip_refused(function: Callable[..., Any], *args: Any) -> Any:
    from redis.exceptions import TimeoutError as RedisTimeoutError

    try:
        return tick.timing.call(function, *args)
    except tick.LedgerRefused as exc:
        name = f"{function.__module__}.{function.__qualname__}"
        print(f"{name} skipped, the ledger refused its write: {exc}", file=sys.stderr)
        return [f"skipped {name}: the ledger refused its write"]
    except RedisTimeoutError as exc:
        name = f"{function.__module__}.{function.__qualname__}"
        print(f"{name} skipped, its Redis read timed out: {exc}", file=sys.stderr)
        return [f"skipped {name}: its Redis read timed out"]


def skip_dropped(function: Callable[..., Any], *args: Any) -> Any:
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError

    for _ in range(2):
        try:
            return tick.timing.call(function, *args) or []
        except (RedisConnectionError, RedisTimeoutError) as exc:
            dropped = exc
    name = f"{function.__module__}.{function.__qualname__}"
    print(f"{name} skipped, its Redis connection dropped or timed out twice: {dropped}", file=sys.stderr)  # pyright: ignore[reportPossiblyUnboundVariable]
    return [f"skipped {name}: its Redis connection dropped or timed out twice"]


def _operator_line(item: Any) -> bool:
    return item.sender == "operator" and not (item.fyi and item.ref.startswith(tick.control_notifications.CONTROL_REF))


def _collected(slug: str, store: Any, ledger: Any, rows: dict[str, Any], agent: Any) -> str:
    if agent.lane == tick.MASTER:
        store.redis.set(store.key(slug, "master-retired-tasks"), json.dumps(list(rows)))
    return tick._drop(slug, store, ledger, rows, agent)


def _drop(slug: str, store: Any, ledger: Any, rows: dict[str, Any], agent: Any, refund: bool = False) -> str:
    held = rows.get(agent.task, {}).get("state") in tick.ACTIVE and rows[agent.task].get("claimed_by") == agent.name
    held = held and not tick.awaiting_merge(rows[agent.task])
    expected = {"claimed_by": agent.name, "claim_generation": rows.get(agent.task, {}).get("claim_generation", 0)}
    refund = refund and held and store.claims(slug, agent.task) > 0
    generation = store.claim_generation(slug, agent.task)
    if refund and tick._still_held(ledger.task(slug, agent.task), expected):
        store.refund_held_claim(slug, agent.task, agent.name, generation)
    store.release(slug, agent.task, agent.name)
    if store.drop_agent(slug, agent.name) is False:
        return DROP_REFUSED
    reopen = held and tick._reopen(slug, ledger, rows, agent.task, expected)
    _settle_exit(store, agent.name, agent.seat if reopen else "")
    return f", task {agent.task} reopened" if reopen else ""


def _settle_exit(store: Any, name: str, seat: str) -> None:
    from scripts.tick.adapters.calls import SpawnStoreCalls

    if isinstance(store, SpawnStoreCalls):
        store.settle_exit(name, seat, "stopped")
    else:
        tick.exits.settle(tick.InboxStore(store.redis), name, seat, "stopped")


def _still_held(row: dict[str, Any] | None, expected: dict[str, Any]) -> bool:
    row = row or {}
    return (
        row.get("state") in tick.ACTIVE
        and row.get("claimed_by") == expected["claimed_by"]
        and row.get("claim_generation", 0) == expected["claim_generation"]
    )


def _follow(slug: str, store: Any, runtime: Any, agent: Any, pid: int | None) -> tuple[Any, list[str]]:
    if pid is None:
        return agent, []
    if (rebound := tick._rebind(slug, store, runtime, agent, pid)) is None:
        return None, [f"held {agent.name} until one pane holds its resumed process {pid}"]
    return rebound, [f"rebound {agent.name} to its resumed process {pid} in pane {rebound.pane_id}"]


def _rebind(slug: str, store: Any, runtime: Any, agent: Any, pid: int) -> Any:
    found: Any = (runtime.conversations() or {}).items()  # pyright: ignore[reportUnknownVariableType]
    panes = [p for p, c in found if c and c == agent.conversation_id and "/" not in p]
    if len(panes) != 1:
        return None
    validation = {**agent.profile_decision.get("validation", {}), "pid": pid}
    rebound = replace(agent, pane_id=panes[0], profile_decision={**agent.profile_decision, "validation": validation})
    store.put_agent(slug, rebound)
    return rebound


def _ended(agent: Any, rows: dict[str, Any]) -> bool:
    task = rows.get(agent.task, {})
    ended = agent.lane != tick.MASTER and (
        task.get("done")
        or task.get("state") in {"done", "blocked", "handoff"}
        or (task.get("awaiting") == "verify" and not task.get("claimed_by"))
        or (not _current_owner(agent, task) and _on_hive(agent))
    )
    return agent.state == "finished" or bool(ended)


def _current_owner(agent: Any, row: dict[str, Any]) -> bool:
    return not claim_lost.lost(agent.name, row, _on_hive(agent))


def _on_hive(agent: Any) -> bool:
    """True for an agent another hive runs, whose emptied claim the tick never restores."""
    from scripts.swarm.commands import hive_id

    return (
        agent.runtime_backend != "local"
        or bool(tick.reports.host(agent))
        or (bool(agent.hive) and agent.hive != hive_id())
    )


def _reopen(slug: str, ledger: Any, rows: dict[str, Any], task_id: str, expected: dict[str, Any] | None = None) -> bool:
    guard = {"if_match": expected} if expected is not None else {}
    live = ledger.update_task(slug, task_id, {"state": "open", "claimed_by": ""}, if_state=tick.ACTIVE, **guard)
    rows[task_id].update(live)
    return live["state"] == "open"


tick: Any = import_module("scripts.swarm.tick")
