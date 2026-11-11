from typing import Any

from scripts.swarm import controller


def take(store: Any, slug: str, held: Any) -> Any:
    # ruleid: tick-tick-lock
    return controller.take_tick_lock(store, slug, held, 30, "place master")


def release(store: Any, slug: str, token: str) -> None:
    # ruleid: tick-tick-lock
    controller.release_tick_lock(store, slug, token, "place master")


def through_the_port(coordination: Any, held: Any, token: str) -> Any:
    # ok: tick-tick-lock
    taken = coordination.tick_lock(held, 30, "place master", deadline=None)
    # ok: tick-tick-lock
    coordination.release_tick_lock(token, "place master", deadline=None)
    return taken
