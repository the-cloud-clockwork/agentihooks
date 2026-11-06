# pyright: reportUnusedFunction=false
"""The master and dispatcher seat steps of the tick pass, run at their four sites in today's order."""

import os
from collections.abc import Callable
from dataclasses import replace
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
from scripts.swarm.store import AgentRecord, RedisStore
from scripts.swarm_v2 import master_scale, master_seat_alarm
from scripts.tick import registry
from scripts.tick.engine import SLOW
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
    CallStep(
        "night watch", WATCH, _held(_night_watch), LISTED | {"sleeping"}, pass_domains("doc", "sleeping"), lane=SLOW
    ),
    CallStep("master alarm", WATCH, _held(_master_alarm), lane=SLOW),
)

for _step in STEPS:
    registry.register(_step)


def _recover_master(slug: Any, config: Any, store: Any, runtime: Any, now_ms: Any) -> Any:
    recorded = [a for a in store.agents(slug) if a.lane == tick.MASTER]
    agents = [a for a in recorded if tick.master_seats.is_lead(slug, a)]
    live = runtime.live_names()
    if any(a.state != "finished" and a.name in live for a in agents):
        return []
    finished = {a.name for a in recorded}
    seat = tick.seat_address(slug, tick.MASTER)
    occupant = store.seats.occupant(seat).occupant
    candidates = []
    for name in sorted(live - finished):
        parsed = tick.parse(name)
        if name == occupant or (parsed and parsed.kind == tick.MASTER and parsed.code == config.code):
            candidates.append(name)  # pyright: ignore[reportUnknownMemberType]
        elif name.startswith(f"{slug}-master-"):
            candidates.append(name)  # pyright: ignore[reportUnknownMemberType]
    if not candidates:
        return []
    name = occupant if occupant in candidates else candidates[0]  # pyright: ignore[reportUnknownVariableType]
    record = store.execution_occupants(slug).get(seat)
    if not record or record.name != name:
        record = AgentRecord(name, tick.MASTER, tick.MASTER, started_at=now_ms, seat=seat)  # pyright: ignore[reportUnknownArgumentType]
    store.put_agent(slug, tick.placed_record(record, runtime.recover(name)))
    for agent in agents:
        if agent.name not in live and agent.state != "finished":
            store.drop_agent(slug, agent.name)
    if occupant != name:
        store.seats.occupy(seat, name, now_ms)
    return [f"adopted live master {name}"]


def _master(slug: Any, config: Any, store: Any, runtime: Any, now_ms: Any, past_host: Any = False) -> Any:
    store.redis.hdel(tick.MASTER_WAITING, slug)
    agents = store.agents(slug)
    masters = [a for a in agents if a.lane == tick.MASTER]
    if config.state == "stopping":
        if any(a.lane != tick.MASTER for a in agents):
            return []
        return [tick._retire_master(slug, store, runtime, m, now_ms) for m in masters]
    masters = [m for m in masters if tick.master_seats.is_lead(slug, m)]
    pending = tick.master_start.read(store, slug)
    if any(m.state != "finished" for m in masters) or tick.master_start.blocked(pending, now_ms):
        return []
    if any(m.name in runtime.live_names() for m in masters):
        return ["the old master is still running, waiting for it to end before starting the next"]
    if refused := tick.seat_spawn.placed_elsewhere(runtime, tick.MASTER, {}):
        return [refused]
    if refused := tick.seat_spawn.no_slot(config, runtime, tick.MASTER):
        store.redis.hset(tick.MASTER_WAITING, slug, now_ms)
        return [refused]
    if not past_host and (held := tick.seat_spawn.host_hold(slug, store, now_ms, tick.MASTER)):
        return [held]

    launched = {}

    def prepare(record: Any) -> Any:
        transfer = tick.transfers.attach(store, slug, record)
        task = (
            {**pending["task"], "transfer": transfer}
            if pending
            else tick.primed(
                store,
                slug,
                record.seat,
                {
                    "id": tick.MASTER,
                    "handoff": store.handoff(slug, tick.MASTER),
                    "peer": store.peer(slug),
                    "transfer": transfer,
                },
            )
        )
        from scripts.swarm import master_launch

        task = master_launch._filled(tick.master_move.pinned(store, slug, task, transfer), config)  # pyright: ignore[reportPrivateUsage, reportUnknownMemberType, reportUnknownVariableType]
        tick.master_start.begin(store, slug, record.name, task, now_ms)
        tick.affinity.handed_off(store, slug)
        launched.update(task)  # pyright: ignore[reportUnknownMemberType]
        return task  # pyright: ignore[reportUnknownVariableType]

    try:
        record, placed = tick.seat_spawn.place(slug, config, store, runtime, tick.MASTER, now_ms, prepare)
    except tick.seat_spawn.SeatFailed as spawn_failure:
        exc, record = spawn_failure.error, spawn_failure.record
        store.drop_agent(slug, record.name)
        if tick.seat_spawn.room_refused(exc):
            tick.transfers.detach(store, slug, record)
            tick.master_start.withdraw(store, slug, pending)
            return [f"the master waits: {exc}"]
        tick.transfers.failed(store, slug, record)
        tick.affinity.failed(store, slug, str(exc))
        return tick.master_start.failed(store, slug, f"master spawn failed: {exc}", now_ms)
    tick.affinity.placed(store, slug, placed.harness)
    tick.master_move.placed(store, slug)
    record = tick.placed_record(record, placed)
    reported = runtime.reported(record)
    store.put_agent(slug, replace(record, state="working" if reported else "starting"))
    tick.launch_check.begin(store, slug, record, now_ms)
    if reported:
        store.redis.delete(store.key(slug, "master-start"))
        store.clear_handoff(slug, tick.MASTER)
    store.redis.hdel(store.key(slug, "launch-assignments"), tick.MASTER)
    return [f"spawned master {record.name}", *tick.master_move.account_moved(slug, launched, record)]


def _retire_master(slug: Any, store: Any, runtime: Any, master: Any, now_ms: Any) -> Any:
    live = master.name in runtime.live_names()
    if held := tick.master_retire.hold(store, slug, master, "the swarm is stopping", live, now_ms):
        return held
    if not tick.retire_watch.retire_unless_gone(
        store, runtime, master, tick.reaper.scratch_homes(slug, master.task), now_ms
    ):
        return f"could not retire {master.name}, retrying next tick"
    store.drop_agent(slug, master.name)
    return f"retired {master.name}"


def respawn_master(slug: Any, store: Any, runtime: Any, now_ms: Any) -> Any:
    from scripts.swarm import controller

    def respawn(config: Any, held: Any) -> Any:
        fenced = controller.FencedRuntime(store, slug, held, runtime, controller.LOCAL)
        live, actions = fenced.live_names(), []
        for agent in store.agents(slug):
            gone = agent.state not in ("finished", "starting") and agent.name not in live
            if agent.lane == tick.MASTER and gone and tick.master_seats.is_lead(slug, agent):
                store.drop_agent(slug, agent.name)
                actions.append(f"dropped the gone master {agent.name}")  # pyright: ignore[reportUnknownMemberType]
        return actions + (
            tick._recover_master(slug, config, store, fenced, now_ms)
            or tick._master(slug, config, store, fenced, now_ms)
        )

    return tick._under_tick_lock(slug, store, respawn, "respawn master")


def place_master(slug: Any, store: Any, runtime: Any, now_ms: Any) -> Any:
    from scripts.swarm import controller

    def place(config: Any, held: Any) -> Any:
        fenced = controller.FencedRuntime(store, slug, held, runtime, controller.deployment_for(config, os.environ))
        return tick._master(slug, config, store, fenced, now_ms)

    return tick._under_tick_lock(slug, store, place, "place master")


def _under_tick_lock(slug: str, store: RedisStore, act: Any, caller: str) -> Any:
    from scripts.swarm import controller, lease
    from scripts.swarm.commands import hive_id

    if store.config(slug).state in ("stopping", "stopped"):
        return []
    held = lease.acquire(store, slug, hive_id())
    if held is None:
        return []
    token = controller.take_tick_lock(store, slug, held, lease.ttl_ms(), caller)
    if token is None:
        return []
    try:
        config = store.ensure_code(slug)  # pyright: ignore[reportUnknownMemberType]
        if config.state in ("stopping", "stopped"):
            return []
        with lease.held_pass(store, slug, held):
            return act(config, held)
    finally:
        controller.release_tick_lock(store, slug, token, caller)


tick: Any = import_module("scripts.swarm.tick")
