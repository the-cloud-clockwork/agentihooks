"""The placement steps of the tick pass, run at their sites in today's order: acks, ranking, preempt and spawn."""

# pyright: reportUnusedFunction=false

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from importlib import import_module
from types import MappingProxyType
from typing import Any

from scripts.swarm import ack_pass, lost_runner, quota_notice, time_left
from scripts.tick import registry
from scripts.tick.engine import SLOW
from scripts.tick.steps import AGENTS, SWARM, TASKS, CallStep, budgets, pass_domains
from scripts.tick.steps.tasks import RANK, WriteStep, held, rank_writes

__all__ = ["ACKS", "NOTICES", "PLACED", "PLACEMENT", "PREEMPT_NEEDS", "STEPS"]

Get = Callable[[str], Any]
NOTICES = "placement notices"
ACKS = "placement acks"
PLACEMENT = "placement"
PREEMPT_NEEDS = frozenset(
    {"reap", "drained resume", "recover outages", "retire idle master", "prerequisite reopen", "fleet red notice"}
)
RESUMED = frozenset({"drained resume"})
CONFIG_DOC = pass_domains("config", "doc")
TASK_STATE = pass_domains("config", "rows", "doc")
PLACED = frozenset({AGENTS, TASKS})


_SPAWN_KEYS = ("slug", "config", "store", "ledger", "runtime", "rows", "doc", "now_ms")


def _quota_notice(tick: Any, get: Get) -> Any:
    keys = ("slug", "config", "store", "ledger", "runtime", "now_ms")
    return tick.skip_refused(quota_notice.refresh, *map(get, keys))


def _lost_runner(tick: Any, get: Get) -> Any:
    keys = ("slug", "config", "store", "now_ms")
    return tick.skip_refused(lost_runner.sweep, *map(get, keys))  # pyright: ignore[reportUnknownMemberType]


def _time_left(tick: Any, get: Get) -> Any:
    keys = ("slug", "store", "ledger", "runtime", "config", "doc")
    return tick.skip_refused(time_left.refresh, *map(get, keys))  # pyright: ignore[reportUnknownMemberType]


def _collect_acks(tick: Any, get: Get) -> Any:
    keys = ("slug", "config", "store", "ledger", "runtime", "doc", "now_ms")
    with ack_pass.collecting(get("store"), get("slug")) as taken:
        return tick.skip_refused(tick._collect_acks, *map(get, keys)) if taken else []


def _codex_hook_order(tick: Any, get: Get) -> Any:
    if not get("sleeping"):
        return tick.skip_refused(tick._codex_hook_order)
    return []


def _rank_pass(tick: Any, get: Get) -> Any:
    keys = ("slug", "config", "store", "ledger", "doc", "now_ms")
    return tick.skip_refused(rank_writes, tick, *map(get, keys))


def _bottleneck_remedy(tick: Any, get: Get) -> Any:
    return tick.skip_refused(
        tick.bottleneck_remedy.step, get("slug"), get("store"), get("ledger"), get("doc"), get("now_ms")
    )


def _preempt(tick: Any, get: Get) -> Any:
    config, store = get("config"), get("store")
    if config.state == "running" and config.engine != tick.EVENTS_ENGINE and tick.panic.state(store) is None:
        placement = partial(tick.preempt.probe, get("runtime"))
        seats = tick.preempt.Seats(get("rows"), tick._ended, tick._current_owner, tick.ready_in_order, placement)
        return tick.skip_refused(tick.preempt.run, get("slug"), config, store, get("doc"), get("now_ms"), seats)
    return []


def _spawn(tick: Any, get: Get) -> Any:
    config, store = get("config"), get("store")
    if (
        config.state == "running"
        and tick.readiness.tick_places(config.engine, get("rows"))
        and tick.panic.state(store) is None
    ):
        rows = dict(get("rows"))
        args = tuple(rows if key == "rows" else get(key) for key in _SPAWN_KEYS)
        with tick._placement_pass(get("runtime")):
            found = tick.skip_refused(tick._spawn, *args)
        get("box")["rows"] = MappingProxyType(rows)
        store.redis.delete(get("due"))
        return found
    return []


@dataclass(frozen=True)
class Started:
    record: Any
    task: dict[str, Any]
    placed: Any
    launched: Any


def _started(
    slug: str, config: Any, store: Any, ledger: Any, doc: dict[str, Any], started: Started, now_ms: int
) -> list[str]:
    record, task, placed = started.record, started.task, started.placed
    store.count_claim(slug, task["id"])
    after = store.take_restart(slug, task["id"]) or tick._master_set(doc, task["id"])
    head = (tick.ledger_events.head(task["pr_url"]) or None) if task.get("pr_url") else ""
    store.record_launch(slug, replace(record, pane_id=placed.pane_id), "started", after=after, head=head)
    store.put_agent(slug, started.launched)
    tick.launch_check.begin(store, slug, record, now_ms)
    store.count_spawn(slug, placed.harness)
    tick._spend_local(store, started.launched, now_ms)
    store.clear_handoff(slug, task["id"])
    store.clear_failed_harness(slug, task["id"])
    store.redis.hdel(store.key(slug, "launch-assignments"), task["id"])
    tick.profile_first.spawned(slug, store, task["id"])
    return [
        f"spawned {record.name} for {task['id']}",
        *tick.dispatcher.chain_claimed(slug, config, ledger, doc, task, record.lane, now_ms),
    ]


@dataclass(frozen=True)
class Failed:
    record: Any
    task: dict[str, Any]
    error: Exception


def _spawn_failed(
    slug: str, store: Any, ledger: Any, runtime: Any, rows: dict[str, Any], failed: Failed, now_ms: int
) -> list[str]:
    record, task, exc = failed.record, failed.task, failed.error
    tick._release_admission(slug, runtime, task)
    tick._release_hive_launch(slug, store, record)
    tick._record_spawn_failure(slug, store, record, exc, now_ms)
    actions: list[str] = []
    if "account reservation is full" in str(exc):
        from scripts.swarm import capacity, quota_handoff

        decision: dict[str, Any] = capacity.read(store, slug)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        readings: list[Any] = decision.get("accounts", []) if decision.get("observed_at") == now_ms else []
        thresholds: Any = quota_handoff.Thresholds.from_env(dict(os.environ))  # pyright: ignore[reportUnknownMemberType]
        snapshot = {
            "harness": getattr(exc, "harness", ""),
            "accounts": [row for row in readings if not quota_handoff.trigger(capacity.Account(**row), thresholds)],
        }
        actions.append(f"spawn capacity for {task['id']}: {json.dumps(snapshot)}")
    actions.append(f"spawn failed for {task['id']}{tick._drop(slug, store, ledger, rows, record)}: {exc}")
    if isinstance(exc, tick.ProfileUnresolved):
        actions.append(tick._unresolved(slug, ledger, rows, task["id"], str(exc)))
    return actions


def _not_launched(
    slug: str, store: Any, ledger: Any, runtime: Any, rows: dict[str, Any], failed: Failed, now_ms: int
) -> list[str]:
    record, task = failed.record, failed.task
    if tick._acknowledging_error(failed.error):
        store.record_launch(slug, record, tick.ACKNOWLEDGING)
        return [f"{record.name} waits for the hive acknowledgement of {task['id']}"]
    if refused := tick._failed_room_wait(runtime, record.lane, task) or tick._refused_reservation(task, failed.error):
        return [tick._room_refused(slug, store, ledger, runtime, rows, failed, refused)]
    return tick._spawn_failed(slug, store, ledger, runtime, rows, failed, now_ms)


def _failed_room_wait(runtime: Any, lane: str, task: dict[str, Any]) -> str | bool:
    try:
        return tick._room_wait(runtime, lane, task)
    except tick.SpawnError:
        return False


def _refused_reservation(task: dict[str, Any], exc: Exception) -> str:
    return f"task {task['id']} waits: {exc}" if tick.seat_spawn.room_refused(exc) else ""


def _room_refused(
    slug: str, store: Any, ledger: Any, runtime: Any, rows: dict[str, Any], failed: Failed, refused: str
) -> str:
    record, task = failed.record, failed.task
    tick._release_admission(slug, runtime, task)
    tick._release_hive_launch(slug, store, record)
    tick.transfers.detach(store, slug, record)
    store.record_launch(slug, record, "waiting", str(failed.error))
    tick._drop(slug, store, ledger, rows, record)
    return refused


def _room_wait(runtime: Any, lane: str, task: dict[str, Any]) -> str:
    try:
        refused = tick.seat_spawn.placed_elsewhere(runtime, lane, task)
    except tick.ProfileUnresolved:
        return ""
    return f"task {task['id']} waits: {refused}" if refused else ""


def _admission(runtime: Any) -> Any:
    from scripts.swarm_v2.admission import PendingAdmission

    gate = getattr(runtime, "admission", None)
    return gate if isinstance(gate, PendingAdmission) else None


def _held_back(slug: str, ledger: Any, rows: dict[str, Any], runtime: Any, task: dict[str, Any], now_ms: int) -> str:
    from scripts.swarm_v2.admission import ADMITTED, IMPOSSIBLE, REPLAYED

    gate = tick._admission(runtime)
    if gate is None:
        return ""
    try:
        decision = gate.admit(slug, [task], now_ms)[0]
    except tick.SwarmError as exc:
        return f"task {task['id']} waits: {exc}"
    if decision.outcome == IMPOSSIBLE:
        return tick._unresolved(slug, ledger, rows, task["id"], decision.reason)
    if decision.outcome not in (ADMITTED, REPLAYED):
        return f"task {task['id']} waits: {decision.reason}"
    task["admission"] = {"reservation": decision.reservation, "deadline_ms": decision.deadline_ms}
    return ""


def _settle_admission(slug: str, runtime: Any, agent: Any) -> None:
    gate = tick._admission(runtime)
    if gate is not None and (reservation := gate.held(slug, agent.task)):
        gate.activate(slug, agent.task, reservation)


def _release_admission(slug: str, runtime: Any, task: dict[str, Any]) -> None:
    held = task.get("admission")
    if held:
        tick._admission(runtime).release(slug, task["id"], held["reservation"])


STEPS = (
    CallStep("quota notice", NOTICES, held(_quota_notice), RESUMED, CONFIG_DOC),
    CallStep("lost runner", NOTICES, held(_lost_runner), RESUMED, pass_domains("config"), lane=SLOW),
    CallStep("time left", NOTICES, held(_time_left), RESUMED, CONFIG_DOC, lane=SLOW),
    CallStep("collect acks", ACKS, held(_collect_acks), RESUMED, CONFIG_DOC),
    CallStep("codex hook order", ACKS, held(_codex_hook_order), frozenset({"sleeping"}), pass_domains("sleeping")),
    WriteStep("rank pass", PLACEMENT, held(_rank_pass), writes=frozenset({RANK}), needs=RESUMED, reads=CONFIG_DOC),
    CallStep(
        "bottleneck remedy", PLACEMENT, held(_bottleneck_remedy), frozenset({"ledger state"}), pass_domains("doc")
    ),
    CallStep(
        "preempt", PLACEMENT, held(_preempt), PREEMPT_NEEDS, frozenset({AGENTS}) | TASK_STATE, frozenset({AGENTS})
    ),
    CallStep(
        "spawn",
        PLACEMENT,
        held(_spawn),
        budgets.SPAWN_NEEDS,
        frozenset({AGENTS, TASKS, SWARM}) | TASK_STATE,
        PLACED | pass_domains("rows"),
    ),
)

for _step in STEPS:
    registry.register(_step)

tick: Any = import_module("scripts.swarm.tick")
