# pyright: reportUnusedFunction=false, reportUnusedClass=false
"""The master and dispatcher seat steps of the tick pass, run at their four sites in today's order."""

from collections.abc import Callable, Mapping
from functools import partial
from importlib import import_module
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
from scripts.tick import engine, registry, steps
from scripts.tick.engine import SLOW
from scripts.tick.step import Domain
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


def _master_down_step(tick: Any, get: Get) -> Any:
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


def _night_watch_step(tick: Any, get: Get) -> Any:
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


def _woken(slug: Any, config: Any, store: Any, ledger: Any) -> Any:
    inbox = tick.InboxStore(store.redis)
    waiting = inbox.pending_items(tick.seat_address(slug, tick.MASTER))
    if waiting and config.template == tick.priming.TEMPLATE and ledger.closed(slug):
        tick.priming.cancel_master_items(inbox, slug)
        return False
    return any(tick._operator_line(item) for item in waiting)


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
    waiting = [item for item in inbox.inbox(seat) if item.state not in tick.CLOSED and tick._operator_line(item)]
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


class _PassView:
    """A view holding the pass state a tick group site hands its steps."""

    def __init__(self, state: dict[str, object]) -> None:
        """Keep the pass state of the group site."""
        self._state = state

    def read(self, domain: Domain, key: str) -> object | None:
        """Return the pass state under ``key``, nothing for any other domain."""
        return self._state.get(key) if domain == steps.PASS else None


def _run_group(name: str, **state: Any) -> list[str]:
    """Run the registered steps of tick group ``name`` with ``state``, applying their task writes, returning actions.

    Actions of slow lane steps that end after the group returns are printed when they end.
    """
    ctx = tick.Context(tick._PassView(state), tick.Deadline.after(tick.GROUP_BUDGET_S))
    box: Mapping[str, object] = state.get("box", {})
    printing = partial(tick._print_actions, box)
    records = engine.run_group(name, ctx, apply=tick.task_steps.appliers(state), finished=printing)
    tick.steps.note_left(box, records)
    return tick.steps.actions(records)


def _print_actions(box: Mapping[str, object], records: tuple[engine.StepRecord, ...]) -> None:
    """Print each action the handed off steps returned, each step that failed with its error, and the exception a
    held step stored in the pass ``box``, one per line."""
    for action in tick.steps.actions(records):
        print(action)
    for record in records:
        if record.error:
            print(f"slow lane step {record.name} failed: {record.error}")
    if "raised" in box:
        print(f"a slow lane step held {box['raised']!r}")


def _codex_hook_order() -> Any:
    from scripts.targets.codex_target import codex_home, restore_hook_order

    home = codex_home()
    moved = restore_hook_order(home)
    path = (home / "hooks.json").resolve()
    return [f"restored the approved Codex hook order in {path}: {', '.join(moved)}"] if moved else []


CONFIGURED = frozenset({"stopped branch"})
RESUMED = frozenset({"drained resume"})
LISTED = frozenset({"ledger state"})
CONFIG = pass_domains("config")

STEPS = (
    CallStep("master wake", MASTERS, _held(_master_wake), CONFIGURED | LISTED, pass_domains("config", "doc")),
    CallStep(
        "retire idle master", MASTERS, _held(_retire_idle_master), frozenset({"spawn"}), pass_domains("rows"), lane=SLOW
    ),
    tasks.WriteStep(
        "master stall",
        MASTERS,
        _held(_master_stall),
        writes=frozenset({tasks.STALL}),
        needs=RESUMED,
        reads=CONFIG,
        lane=SLOW,
    ),
    CallStep("prompt stall", MASTERS, _held(_prompt_stall), RESUMED, CONFIG, lane=SLOW),
    CallStep("master down", SEATS, _held(tasks.placing(_master_down_step)), RESUMED, CONFIG),
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
    CallStep(
        "night watch",
        WATCH,
        _held(_night_watch_step),
        LISTED | {"sleeping"},
        pass_domains("doc", "sleeping"),
        lane=SLOW,
    ),
    CallStep("master alarm", WATCH, _held(_master_alarm), lane=SLOW),
)

for _step in STEPS:
    registry.register(_step)

tick: Any = import_module("scripts.swarm.tick")
