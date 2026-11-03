"""The master and dispatcher seat steps of the tick pass, run at their four sites in today's order."""

from collections.abc import Callable, Mapping
from functools import partial
from importlib import import_module
from types import SimpleNamespace
from typing import Any

from scripts.swarm import (
    control_notifications,
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
from scripts.tick.steps import PASS, CallStep, Left, PassBox, actions, budgets

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

tick: Any = import_module("scripts.swarm.tick")


def _operator_line(item: Any) -> bool:
    return item.sender == "operator" and not (item.fyi and item.ref.startswith(control_notifications.CONTROL_REF))


def _master_down(slug: Any, config: Any, store: Any, ledger: Any, runtime: Any, now_ms: Any) -> Any:
    """Operator lines waiting at a master seat nobody holds: tell the chat once per line, and return any line a dead
    master took but never closed to pending so the next master receives it."""
    live = runtime.live_names()
    masters = [a for a in store.agents(slug) if a.lane == tick.MASTER and a.state != "finished"]
    if config.state == "stopping" or any(
        m.name in live or now_ms - m.started_at <= tick.STARTUP_GRACE_MS for m in masters
    ):
        return []
    inbox, seat = tick.InboxStore(store.redis), tick.seat_address(slug, tick.MASTER)
    waiting = [item for item in inbox.inbox(seat) if item.state not in tick.CLOSED and _operator_line(item)]
    for item in waiting:
        if item.state != "pending":
            inbox.redirect(item.id, "swarm", seat, tick.REDELIVERED)
    told = [item for item in waiting if tick.DOWN_TOLD not in (e.get("event") for e in inbox.history(item.id))]
    for item in told:
        inbox.note(item.id, tick.DOWN_TOLD, "swarm", tick.MASTER_DOWN, now_ms)
    if not told:
        return []
    ledger.notify(slug, tick.MASTER_DOWN)
    return [f"master down, told the operator about {len(told)} waiting lines"]


def _night_watch(slug: Any, store: Any, ledger: Any, runtime: Any, doc: Any, now_ms: Any, sleeping: Any) -> Any:
    from scripts.swarm.health import night_watch

    def respawn() -> Any:
        if tick.master_start.read(store, slug).get("alerted"):
            store.redis.delete(store.key(slug, "master-start"))
        with tick.PLACING:
            return tick._master(slug, store.config(slug), store, runtime, now_ms, past_host=True)

    return night_watch.run(night_watch.Watch(store, slug, ledger, runtime, doc, now_ms, sleeping, respawn))


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
    box: dict[str, Any] = PassBox()
    records = engine.run_group(group, Context(_SiteView(found, box), Deadline.after(budgets.SLOW_S)))
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
        except Left:
            return []
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


def _master_down_step(tick: Any, get: Get) -> Any:
    return tick.skip_refused(
        _master_down, get("slug"), get("config"), get("store"), get("ledger"), get("runtime"), get("now_ms")
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


def _night_watch_step(tick: Any, get: Get) -> Any:
    return tick.skip_refused(
        _night_watch,
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
    CallStep("master wake", MASTERS, _held(_master_wake)),
    CallStep("retire idle master", MASTERS, _held(_retire_idle_master)),
    CallStep("master stall", MASTERS, _held(_master_stall)),
    CallStep("prompt stall", MASTERS, _held(_prompt_stall)),
    CallStep("master down", SEATS, _held(_master_down_step)),
    CallStep("tick master", SEATS, _held(_tick_master)),
    CallStep("master scale", SEATS, _held(_master_scale)),
    CallStep("seat alarm", SEATS, _held(_seat_alarm)),
    CallStep("dispatch seat", DISPATCH, _held(_dispatch_seat)),
    CallStep("night watch", WATCH, _held(_night_watch_step)),
    CallStep("master alarm", WATCH, _held(_master_alarm)),
)

for _step in STEPS:
    registry.register(_step)
