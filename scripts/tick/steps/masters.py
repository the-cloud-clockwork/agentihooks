"""The master and dispatcher seat steps of the tick pass, run at their four sites in today's order."""

from collections.abc import Callable
from types import SimpleNamespace
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
from scripts.tick.steps import CallStep, Left, pass_domains, tasks

__all__ = [
    "DISPATCH",
    "MASTERS",
    "RAISED",
    "SEATS",
    "STEPS",
    "WATCH",
]

Get = Callable[[str], Any]
MASTERS = "masters"
SEATS = "master seats"
DISPATCH = "dispatch seat"
WATCH = "master watch"
RAISED = "raised"
BOX = "box"
QUIET_STATES = frozenset({"stopped", "stopping"})
SEAT: Any = SimpleNamespace(
    dispatch_seat=dispatch_seat,
    lifetime=lifetime,
    master_alarm=master_alarm,
    master_prompt_stall=master_prompt_stall,
    master_scale=master_scale,
    master_seat_alarm=master_seat_alarm,
    master_stall=master_stall,
    master_wake=master_wake,
    tick_master=tick_master,
)


def _held(call: Callable[[Any, Get], Any]) -> Callable[[Any, Get], Any]:
    """Wrap ``call`` to skip once a step held an exception and to hold the exception it raises."""

    def run(tick: Any, get: Get) -> Any:
        box = get(BOX)
        if RAISED in box:
            return []
        try:
            return call(tick, get)
        except Left:
            return []
        except (Exception, SystemExit) as exc:
            box[RAISED] = exc
            return []

    return run


def _master_wake(tick: Any, get: Get) -> Any:
    if get("config").state in QUIET_STATES:
        return []
    return tick.skip_refused(SEAT.master_wake.run, get("slug"), get("store"), get("runtime"), get("doc"), get("now_ms"))


def _retire_idle_master(tick: Any, get: Get) -> Any:
    return tick.skip_refused(
        SEAT.lifetime.retire_idle_master,
        get("slug"),
        get("store"),
        get("ledger"),
        get("runtime"),
        get("rows"),
        get("now_ms"),
    )


def _master_stall(tick: Any, get: Get) -> Any:
    return tasks.stall_writes(
        tick.skip_refused(
            SEAT.master_stall.stalls,
            get("slug"),
            get("config"),
            get("store"),
            get("ledger"),
            get("runtime"),
            get("now_ms"),
        )
    )


def _prompt_stall(tick: Any, get: Get) -> Any:
    return tick.skip_refused(
        SEAT.master_prompt_stall.run,
        get("slug"),
        get("config"),
        get("store"),
        get("ledger"),
        get("runtime"),
        get("now_ms"),
    )


def _master_down(tick: Any, get: Get) -> Any:
    return tick.skip_refused(
        tick._master_down, get("slug"), get("config"), get("store"), get("ledger"), get("runtime"), get("now_ms")
    )


def _tick_master(tick: Any, get: Get) -> Any:
    return tick.skip_refused(
        SEAT.tick_master.run,
        get("slug"),
        get("store"),
        get("ledger"),
        get("runtime"),
        get("now_ms"),
        lambda: tick._master(get("slug"), get("config"), get("store"), get("runtime"), get("now_ms")),
        get("doc"),
    )


def _master_scale(tick: Any, get: Get) -> Any:
    return tick.skip_refused(
        SEAT.master_scale.run, get("slug"), get("config"), get("store"), get("runtime"), get("doc"), get("now_ms")
    )


def _seat_alarm(tick: Any, get: Get) -> Any:
    return tick.skip_refused(
        SEAT.master_seat_alarm.run, get("slug"), get("store"), get("ledger"), get("runtime"), get("doc")
    )


def _dispatch_seat(tick: Any, get: Get) -> Any:
    return tick.skip_refused(
        SEAT.dispatch_seat.run,
        get("slug"),
        get("config"),
        get("store"),
        get("runtime"),
        get("doc"),
        get("now_ms"),
        get("sleeping"),
    )


def _night_watch(tick: Any, get: Get) -> Any:
    return tick.skip_refused(
        tick._night_watch,
        get("slug"),
        get("store"),
        get("ledger"),
        get("runtime"),
        get("doc"),
        get("now_ms"),
        get("sleeping"),
    )


def _master_alarm(tick: Any, get: Get) -> Any:
    promoted = SEAT.tick_master.promoted(get("store"), get("slug"))
    return tick.skip_refused(SEAT.master_alarm.run, get("slug"), get("store"), get("runtime"), promoted)


CONFIGURED = frozenset({"stopped branch"})
RESUMED = frozenset({"drained resume"})
LISTED = frozenset({"ledger state"})
CONFIG = pass_domains("config")

STEPS = (
    CallStep("master wake", MASTERS, _held(_master_wake), CONFIGURED | LISTED, pass_domains("config", "doc")),
    CallStep("retire idle master", MASTERS, _held(_retire_idle_master), frozenset({"spawn due"}), pass_domains("rows")),
    tasks.WriteStep(
        "master stall", MASTERS, _held(_master_stall), writes=frozenset({tasks.STALL}), needs=CONFIGURED, reads=CONFIG
    ),
    CallStep("prompt stall", MASTERS, _held(_prompt_stall), CONFIGURED, CONFIG),
    CallStep("master down", SEATS, _held(tasks.placing(_master_down)), RESUMED, CONFIG),
    CallStep("tick master", SEATS, _held(tasks.placing(_tick_master)), RESUMED, pass_domains("config", "doc")),
    CallStep("master scale", SEATS, _held(tasks.placing(_master_scale)), RESUMED, pass_domains("config", "doc")),
    CallStep("seat alarm", SEATS, _held(tasks.placing(_seat_alarm)), LISTED, pass_domains("doc")),
    CallStep(
        "dispatch seat",
        DISPATCH,
        _held(tasks.placing(_dispatch_seat)),
        RESUMED | {"sleeping"},
        pass_domains("config", "doc", "sleeping"),
    ),
    CallStep("night watch", WATCH, _held(_night_watch), LISTED | {"sleeping"}, pass_domains("doc", "sleeping")),
    CallStep("master alarm", WATCH, _held(_master_alarm)),
)

for _step in STEPS:
    registry.register(_step)
