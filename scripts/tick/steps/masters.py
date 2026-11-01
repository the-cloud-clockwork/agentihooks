"""The master and dispatcher seat steps of the tick pass, run at their sites in today's order."""

from collections.abc import Callable
from typing import Any

from scripts.swarm import (
    dispatch_seat,
    lifetime,
    master_alarm,
    master_prompt_stall,
    master_stall,
    master_wake,
    tick_master,
)
from scripts.swarm_v2 import master_scale, master_seat_alarm
from scripts.tick import registry
from scripts.tick.steps import CallStep

__all__ = ["DISPATCH", "MASTERS", "RAISED", "SEATS", "STEPS", "WATCH", "raise_held"]

Get = Callable[[str], Any]
MASTERS = "masters"
SEATS = "master seats"
DISPATCH = "dispatch seat"
WATCH = "master watch"
RAISED = "raised"


def _held(call: Callable[[Any, Get], Any]) -> Callable[[Any, Get], Any]:
    """Wrap ``call`` to skip once a step held an exception and to hold the exception it raises."""

    def run(tick: Any, get: Get) -> Any:
        box = get("box")
        if RAISED in box:
            return []
        try:
            return call(tick, get)
        except (Exception, SystemExit) as exc:
            box[RAISED] = exc
            return []

    return run


def raise_held(box: dict[str, Any]) -> None:
    """Raise the exception a step held, as the hand written pass let it escape."""
    if RAISED in box:
        raise box[RAISED]


def _master_wake(tick: Any, get: Get) -> Any:
    if get("config").state in {"stopped", "stopping"}:
        return []
    return tick.skip_refused(master_wake.run, get("slug"), get("store"), get("runtime"), get("doc"), get("now_ms"))


def _retire_idle_master(tick: Any, get: Get) -> Any:
    args = (get("slug"), get("store"), get("ledger"), get("runtime"), get("rows"), get("now_ms"))
    return tick.skip_refused(lifetime.retire_idle_master, *args)


def _master_stall(tick: Any, get: Get) -> Any:
    args = (get("slug"), get("config"), get("store"), get("ledger"), get("runtime"), get("now_ms"))
    return tick.skip_refused(master_stall.run, *args)


def _prompt_stall(tick: Any, get: Get) -> Any:
    args = (get("slug"), get("config"), get("store"), get("ledger"), get("runtime"), get("now_ms"))
    return tick.skip_refused(master_prompt_stall.run, *args)


def _master_down(tick: Any, get: Get) -> Any:
    args = (get("slug"), get("config"), get("store"), get("ledger"), get("runtime"), get("now_ms"))
    return tick.skip_refused(tick._master_down, *args)


def _tick_master(tick: Any, get: Get) -> Any:
    slug, config, store, runtime, now_ms = get("slug"), get("config"), get("store"), get("runtime"), get("now_ms")
    return tick.skip_refused(
        tick_master.run,
        slug,
        config,
        store,
        get("ledger"),
        runtime,
        now_ms,
        lambda: tick._master(slug, config, store, runtime, now_ms),
    )


def _master_scale(tick: Any, get: Get) -> Any:
    args = (get("slug"), get("config"), get("store"), get("runtime"), get("doc"), get("now_ms"))
    return tick.skip_refused(master_scale.run, *args)


def _seat_alarm(tick: Any, get: Get) -> Any:
    args = (get("slug"), get("store"), get("ledger"), get("runtime"), get("doc"))
    return tick.skip_refused(master_seat_alarm.run, *args)


def _dispatch_seat(tick: Any, get: Get) -> Any:
    args = (get("slug"), get("config"), get("store"), get("runtime"), get("doc"), get("now_ms"), get("sleeping"))
    return tick.skip_refused(dispatch_seat.run, *args)


def _night_watch(tick: Any, get: Get) -> Any:
    args = (get("slug"), get("store"), get("ledger"), get("runtime"), get("doc"), get("now_ms"), get("sleeping"))
    return tick.skip_refused(tick._night_watch, *args)


def _master_alarm(tick: Any, get: Get) -> Any:
    slug, store = get("slug"), get("store")
    return tick.skip_refused(master_alarm.run, slug, store, get("runtime"), tick_master.promoted(store, slug))


STEPS = (
    CallStep("master wake", MASTERS, 10, _held(_master_wake)),
    CallStep("retire idle master", MASTERS, 20, _held(_retire_idle_master)),
    CallStep("master stall", MASTERS, 30, _held(_master_stall)),
    CallStep("prompt stall", MASTERS, 40, _held(_prompt_stall)),
    CallStep("master down", SEATS, 10, _held(_master_down)),
    CallStep("tick master", SEATS, 20, _held(_tick_master)),
    CallStep("master scale", SEATS, 30, _held(_master_scale)),
    CallStep("seat alarm", SEATS, 40, _held(_seat_alarm)),
    CallStep("dispatch seat", DISPATCH, 10, _held(_dispatch_seat)),
    CallStep("night watch", WATCH, 10, _held(_night_watch)),
    CallStep("master alarm", WATCH, 20, _held(_master_alarm)),
)

for _step in STEPS:
    registry.register(_step)
