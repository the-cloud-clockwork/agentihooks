"""The placement steps of the tick pass, run at their sites in today's order: acks, ranking, preempt and spawn."""

from collections.abc import Callable
from contextlib import contextmanager
from functools import partial
from importlib import import_module
from types import MappingProxyType
from typing import Any

from scripts.swarm import lost_runner, quota_notice, time_left
from scripts.tick import registry
from scripts.tick.steps import AGENTS, SWARM, TASKS, CallStep, budgets
from scripts.tick.steps.tasks import RANK, WriteStep, held, rank_writes

__all__ = ["ACKS", "NOTICES", "PLACED", "PLACEMENT", "PREEMPT_NEEDS", "STEPS"]

Get = Callable[[str], Any]

tick: Any = import_module("scripts.swarm.tick")


def _codex_hook_order() -> Any:
    from scripts.targets.codex_target import codex_home, restore_hook_order

    home = codex_home()
    moved = restore_hook_order(home)
    path = (home / "hooks.json").resolve()
    return [f"restored the approved Codex hook order in {path}: {', '.join(moved)}"] if moved else []


@contextmanager
def _placement_pass(runtime: Any) -> Any:
    try:
        yield
    finally:
        if end := getattr(runtime, "end_placement_pass", None):
            end()


def _spawn(
    slug: str,
    config: Any,
    store: Any,
    ledger: Any,
    runtime: Any,
    rows: dict[str, Any],
    doc: dict[str, Any],
    now_ms: int,
) -> list[str]:
    agents = store.agents(slug)
    taken = {a.seat for a in agents}
    held = tick.timing.timed(tick._held_for_master, slug, store, now_ms)
    order, actions = tick.timing.timed(tick._spawn_order, slug, config, store, agents, rows, doc, runtime)
    actions += tick.timing.timed(tick._block_unresolved, slug, ledger, rows, order)
    order = [(lane, task) for lane, task in order if tick.profile_first.UNRESOLVED not in task]
    run = tick._Pass(slug, config, store, ledger, runtime, rows, doc, now_ms)
    batch: list[Any]
    clock, batch = tick._LaunchClock(), []
    for lane, task in order:
        stop = held or [
            tick.timing.timed(tick._spawn_stop, slug, config, store, runtime, now_ms, len(batch))
            or clock.spent(task, batch)
        ]
        if any(stop):
            return actions + tick._launch_all(run, clock, batch) + stop
        at = tick.time.monotonic()
        launch, prepared = tick._prepare(run, lane, task, taken, clock.failing, {peer.record.name for peer in batch})
        clock.prepared(at)
        actions += prepared
        batch += [launch] if launch else []
        if batch and len(batch) >= tick._launch_room(runtime, config):
            actions += tick._launch_all(run, clock, batch)
            batch = []
    return actions + tick._launch_all(run, clock, batch)


def _collect_acks(
    slug: str, config: Any, store: Any, ledger: Any, runtime: Any, doc: dict[str, Any], now_ms: int
) -> list[str]:
    from scripts.swarm_v2.runtime import codex

    launch_job: Any = import_module("scripts.swarm.launch_job")
    rows = {t["id"]: t for t in doc["tasks"]}
    actions: list[str]
    pending: set[str]
    actions, pending = (
        ([], set())
        if config.engine == tick.EVENTS_ENGINE
        else launch_job.collect(tick._Pass(slug, config, store, ledger, runtime, rows, doc, now_ms))
    )
    for agent in [
        a
        for a in store.agents(slug)
        if tick.readiness.tick_launches(config.engine, rows.get(a.task, {}))
        and tick._acknowledging(slug, store, a)
        and a.name not in pending
    ]:
        task = rows.get(agent.task) or {"id": agent.task}
        try:
            placed = runtime.collect(config, agent.lane, agent.name, {**task, "seat": agent.seat, tick.ACK_LATER: True})
            if agent.state == "finished":
                actions.append(tick._finished_launch(slug, store, doc, agent))
                continue
            launched = codex.admit_legacy(store, slug, tick.placed_record(agent, placed), runtime)
        except Exception as exc:
            error = exc
            transient = tick._acknowledging_error(exc) or not isinstance(exc, tick.SpawnError | tick.SwarmError)
            if transient and now_ms - agent.started_at >= tick.LEASE_MS:
                error = tick._expire_launch(config, runtime, agent, error)
            if tick._acknowledging_error(error):
                store.refresh(slug, agent.task, agent.name, tick.LEASE_MS)
            elif isinstance(error, tick.SpawnError | tick.SwarmError):
                actions += tick._spawn_failed(
                    slug, store, ledger, runtime, rows, tick.Failed(agent, task, error), now_ms
                )
            else:
                print(
                    f"{agent.name} collect for {task['id']} raised {error!r}, waiting for the hive",
                    file=tick.sys.stderr,
                )
                store.refresh(slug, agent.task, agent.name, tick.LEASE_MS)
            continue
        actions += tick._started(slug, config, store, ledger, doc, tick.Started(agent, task, placed, launched), now_ms)
    return actions


NOTICES = "placement notices"
ACKS = "placement acks"
PLACEMENT = "placement"
PREEMPT_NEEDS = frozenset({"reap"})
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


def _collect_acks_step(tick: Any, get: Get) -> Any:
    keys = ("slug", "config", "store", "ledger", "runtime", "doc", "now_ms")
    return tick.skip_refused(_collect_acks, *map(get, keys))


def _codex_hook_order_step(tick: Any, get: Get) -> Any:
    if not get("sleeping"):
        return tick.skip_refused(_codex_hook_order)
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


def _spawn_step(tick: Any, get: Get) -> Any:
    config, store = get("config"), get("store")
    if (
        config.state == "running"
        and tick.readiness.tick_places(config.engine, get("rows"))
        and tick.panic.state(store) is None
    ):
        rows = dict(get("rows"))
        args = tuple(rows if key == "rows" else get(key) for key in _SPAWN_KEYS)
        with _placement_pass(get("runtime")):
            found = tick.skip_refused(_spawn, *args)
        get("box")["rows"] = MappingProxyType(rows)
        store.redis.delete(get("due"))
        return found
    return []


STEPS = (
    CallStep("quota notice", NOTICES, held(_quota_notice)),
    CallStep("lost runner", NOTICES, held(_lost_runner)),
    CallStep("time left", NOTICES, held(_time_left)),
    CallStep("collect acks", ACKS, held(_collect_acks_step)),
    CallStep("codex hook order", ACKS, held(_codex_hook_order_step)),
    WriteStep("rank pass", PLACEMENT, held(_rank_pass), writes=frozenset({RANK})),
    CallStep("bottleneck remedy", PLACEMENT, held(_bottleneck_remedy)),
    CallStep("preempt", PLACEMENT, held(_preempt), PREEMPT_NEEDS, frozenset({AGENTS}), frozenset({AGENTS})),
    CallStep("spawn", PLACEMENT, held(_spawn_step), budgets.SPAWN_NEEDS, frozenset({AGENTS, TASKS, SWARM}), PLACED),
)

for _step in STEPS:
    registry.register(_step)
