from collections.abc import Callable
from typing import Any

from scripts.tick.engine import _abandoned


def journal_at_lane_end(running: dict[Any, Any], records: dict[str, float], clock: Callable[[], float]) -> None:
    for future, (step, started, lane) in list(running.items()):
        # ruleid: abandon-at-lane-expiry
        if lane.expired:
            running.pop(future)
            _abandoned(step)
            records[step.name] = clock() - started


def close_at_lane_end(running: dict[Any, Any], records: dict[str, float | None]) -> None:
    for future, (step, start, lane) in list(running.items()):
        # ok: abandon-at-lane-expiry
        if lane.expired:
            running.pop(future)
            records[step.name] = start.close()
