"""The placement steps of the tick pass, run at their sites in today's order: acks, ranking, preempt and spawn."""

from collections.abc import Callable
from functools import partial
from typing import Any

from scripts.swarm import bottleneck_remedy, panic
from scripts.tick import registry
from scripts.tick.steps import CallStep
from scripts.tick.steps.tasks import held

__all__ = ["ACKS", "DUE", "PLACEMENT", "STEPS"]

Get = Callable[[str], Any]
DUE = "due placement"
ACKS = "placement acks"
PLACEMENT = "placement"


def _spawn_args(get: Get) -> tuple[Any, ...]:
    keys = ("slug", "config", "store", "ledger", "runtime", "rows", "doc", "now_ms")
    return tuple(get(key) for key in keys)


def _due_spawn(tick: Any, get: Get) -> Any:
    with tick.PLACING, tick._placement_pass(get("runtime")):
        return tick.skip_refused(tick._spawn, *_spawn_args(get))


def _collect_acks(tick: Any, get: Get) -> Any:
    keys = ("slug", "config", "store", "ledger", "runtime", "doc", "now_ms")
    return tick.skip_refused(tick._collect_acks, *(get(key) for key in keys))


def _codex_hook_order(tick: Any, get: Get) -> Any:
    if get("sleeping"):
        return []
    return tick.skip_refused(tick._codex_hook_order)


def _rank_pass(tick: Any, get: Get) -> Any:
    args = (get("slug"), get("config"), get("store"), get("ledger"), get("doc"), get("now_ms"))
    return tick.skip_refused(tick.dispatcher.rank_pass, *args)


def _bottleneck_remedy(tick: Any, get: Get) -> Any:
    args = (get("slug"), get("store"), get("ledger"), get("doc"), get("now_ms"))
    return tick.skip_refused(bottleneck_remedy.step, *args)


def _preempt(tick: Any, get: Get) -> Any:
    config, store = get("config"), get("store")
    if config.state != "running" or config.engine == tick.EVENTS_ENGINE or panic.state(store) is not None:
        return []
    placement = partial(tick.preempt.probe, get("runtime"))
    seats = tick.preempt.Seats(get("rows"), tick._ended, tick._current_owner, tick.ready_in_order, placement)
    return tick.skip_refused(tick.preempt.run, get("slug"), config, store, get("doc"), get("now_ms"), seats)


def _spawn(tick: Any, get: Get) -> Any:
    config, store = get("config"), get("store")
    if not (
        config.state == "running"
        and tick.readiness.tick_places(config.engine, get("rows"))
        and panic.state(store) is None
    ):
        return []
    with tick._placement_pass(get("runtime")):
        found = tick.skip_refused(tick._spawn, *_spawn_args(get))
    store.redis.delete(get("due"))
    return found


STEPS = (
    CallStep("due spawn", DUE, 10, held(_due_spawn)),
    CallStep("collect acks", ACKS, 10, held(_collect_acks)),
    CallStep("codex hook order", ACKS, 20, held(_codex_hook_order)),
    CallStep("rank pass", PLACEMENT, 10, held(_rank_pass)),
    CallStep("bottleneck remedy", PLACEMENT, 20, held(_bottleneck_remedy)),
    CallStep("preempt", PLACEMENT, 30, held(_preempt)),
    CallStep("spawn", PLACEMENT, 40, held(_spawn)),
)

for _step in STEPS:
    registry.register(_step)
