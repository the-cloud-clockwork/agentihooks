from typing import Any

from scripts.swarm import controller
from scripts.swarm import controller as lock_owner
from scripts.swarm.controller import release_tick_lock


def take(store: Any, slug: str, held: Any) -> Any:
    # ruleid: tick-tick-lock
    return controller.take_tick_lock(store, slug, held, 30, "place master")


def release(store: Any, slug: str, token: str) -> None:
    # ruleid: tick-tick-lock
    controller.release_tick_lock(store, slug, token, "place master")


def renamed(swarm: Any, slug: str, held: Any) -> Any:
    # ruleid: tick-tick-lock
    return controller.take_tick_lock(swarm, slug, held, 30, "respawn master")


def imported(swarm: Any, slug: str, token: str) -> None:
    # ruleid: tick-tick-lock
    release_tick_lock(swarm, slug, token, "respawn master")


def aliased(swarm: Any, slug: str, held: Any) -> Any:
    # ruleid: tick-tick-lock
    return lock_owner.take_tick_lock(swarm, slug, held, 30, "respawn master")


def through_the_port(coordination: Any, held: Any, token: str) -> Any:
    # ok: tick-tick-lock
    taken = coordination.tick_lock(held, 30, "place master", deadline=None)
    # ok: tick-tick-lock
    coordination.release_tick_lock(token, "place master", deadline=None)
    return taken
