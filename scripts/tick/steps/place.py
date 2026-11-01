"""The placement steps of the tick pass, run at their sites in today's order: acks, ranking, preempt and spawn."""

from collections.abc import Callable
from functools import partial
from typing import Any

import scripts.tick.steps.agents  # noqa: F401
import scripts.tick.steps.observe  # noqa: F401
import scripts.tick.steps.report  # noqa: F401
from scripts.tick import registry
from scripts.tick.steps import AGENTS, SWARM, TASKS, CallStep, budgets
from scripts.tick.steps.tasks import held

__all__ = ["ACKS", "PLACED", "PLACEMENT", "PREEMPT_NEEDS", "STEPS"]

Get = Callable[[str], Any]
ACKS = "placement acks"
PLACEMENT = "placement"
PREEMPT_NEEDS = frozenset({"reap"})
PLACED = frozenset({AGENTS, TASKS})


def _spawn_args(get: Get) -> tuple[Any, ...]:
    keys = ("slug", "config", "store", "ledger", "runtime", "rows", "doc", "now_ms")
    return tuple(get(key) for key in keys)


def _collect_acks(tick: Any, get: Get) -> Any:
    keys = ("slug", "config", "store", "ledger", "runtime", "doc", "now_ms")
    return tick.skip_refused(tick._collect_acks, *map(get, keys))


def _codex_hook_order(tick: Any, get: Get) -> Any:
    if not get("sleeping"):
        return tick.skip_refused(tick._codex_hook_order)
    return []


def _rank_pass(tick: Any, get: Get) -> Any:
    return tick.skip_refused(
        tick.dispatcher.rank_pass, get("slug"), get("config"), get("store"), get("ledger"), get("doc"), get("now_ms")
    )


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
        with tick._placement_pass(get("runtime")):
            found = tick.skip_refused(tick._spawn, *_spawn_args(get))
        store.redis.delete(get("due"))
        return found
    return []


STEPS = (
    CallStep("collect acks", ACKS, held(_collect_acks)),
    CallStep("codex hook order", ACKS, held(_codex_hook_order)),
    CallStep("rank pass", PLACEMENT, held(_rank_pass)),
    CallStep("bottleneck remedy", PLACEMENT, held(_bottleneck_remedy)),
    CallStep("preempt", PLACEMENT, held(_preempt), PREEMPT_NEEDS, frozenset({AGENTS}), frozenset({AGENTS})),
    CallStep("spawn", PLACEMENT, held(_spawn), budgets.SPAWN_NEEDS, frozenset({AGENTS, TASKS, SWARM}), PLACED),
)

for _step in STEPS:
    registry.register(_step)
