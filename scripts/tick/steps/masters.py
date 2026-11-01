"""The master and dispatcher seat steps of the tick pass, run at their four sites in today's order."""

from collections.abc import Callable, Mapping
from functools import partial
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
from scripts.tick import engine, registry
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain
from scripts.tick.steps import BUDGET_S, PASS, CallStep, actions

__all__ = [
    "DISPATCH",
    "MASTERS",
    "RAISED",
    "SEATS",
    "STEPS",
    "WATCH",
    "raise_held",
    "run_dispatch",
    "run_masters",
    "run_seats",
    "run_site",
    "run_watch",
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


class _SiteView:
    """A view holding the tick's locals at a site and the box its steps hold an exception in."""

    def __init__(self, found: Mapping[str, Any], box: dict[str, Any]) -> None:
        """Keep the site's locals and the box."""
        self._found = found
        self._box = box

    def read(self, domain: Domain, key: str) -> object | None:
        """Return the box or the local under ``key`` for the pass domain, nothing for any other domain."""
        if domain != PASS:
            return None
        return self._box if key == BOX else self._found[key]


def run_site(group: str, found: Mapping[str, Any]) -> list[str]:
    """Run tick group ``group`` against the tick's locals ``found``, raising what a step held, returning actions."""
    box: dict[str, Any] = {}
    records = engine.run_group(group, Context(_SiteView(found, box), Deadline.after(BUDGET_S)))
    raise_held(box)
    return actions(records)


run_masters = partial(run_site, MASTERS)
run_seats = partial(run_site, SEATS)
run_dispatch = partial(run_site, DISPATCH)
run_watch = partial(run_site, WATCH)


def _held(call: Callable[[Any, Get], Any]) -> Callable[[Any, Get], Any]:
    """Wrap ``call`` to skip once a step held an exception and to hold the exception it raises."""

    def run(tick: Any, get: Get) -> Any:
        box = get(BOX)
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
    return tick.skip_refused(
        SEAT.master_stall.run, get("slug"), get("config"), get("store"), get("ledger"), get("runtime"), get("now_ms")
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
        get("config"),
        get("store"),
        get("ledger"),
        get("runtime"),
        get("now_ms"),
        lambda: tick._master(get("slug"), get("config"), get("store"), get("runtime"), get("now_ms")),
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
