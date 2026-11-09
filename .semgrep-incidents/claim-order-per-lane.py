from typing import Any

from scripts.swarm.tick import ready_in_order


def spawn_order(tick: Any, timing: Any, pass_args: tuple[Any, ...], caps: dict[str, int]) -> list[Any]:
    queue = []
    for lane, cap in caps.items():
        # ruleid: claim-order-per-lane
        ready = timing.timed(tick.ready_in_order, *pass_args, lane)
        queue += ready[:cap]
    return queue


def each_lane(pass_args: tuple[Any, ...], lanes: list[str]) -> dict[str, Any]:
    found = {}
    for lane in lanes:
        # ruleid: claim-order-per-lane
        found[lane] = ready_in_order(*pass_args, lane)
    return found


def by_lane(pass_args: tuple[Any, ...], lanes: list[str]) -> dict[str, Any]:
    # ruleid: claim-order-per-lane
    return {lane: ready_in_order(*pass_args, lane) for lane in lanes}


def once_per_pass(tick: Any, timing: Any, pass_args: tuple[Any, ...], caps: dict[str, int]) -> list[Any]:
    # ok: claim-order-per-lane
    lanes = timing.timed(tick.ready_by_lane, *pass_args, list(caps))
    queue = []
    for lane, cap in caps.items():
        queue += lanes[lane][:cap]
    return queue


def one_lane(tick: Any, pass_args: tuple[Any, ...], lane: str) -> Any:
    # ok: claim-order-per-lane
    return tick.ready_in_order(*pass_args, lane)
