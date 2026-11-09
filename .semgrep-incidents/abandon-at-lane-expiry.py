from collections.abc import Callable
from typing import Any

from scripts.tick.engine import StepRecord, _abandoned, _left


def journal_at_lane_end(running: dict[Any, Any], records: dict[str, float], clock: Callable[[], float]) -> None:
    for future, (step, started, lane) in list(running.items()):
        if lane.expired:
            running.pop(future)
            # ruleid: abandon-at-lane-expiry
            _abandoned(step)
            records[step.name] = clock() - started


def record_abandoned_at_lane_end(running: dict[Any, Any], records: dict[str, Any], clock: Callable[[], float]) -> None:
    for future, (step, started, lane) in list(running.items()):
        if lane.expired:
            running.pop(future)
            # ruleid: abandon-at-lane-expiry
            records[step.name] = StepRecord(step.name, clock() - started, step.budget_s, abandoned=True)


def left_at_lane_end(running: dict[Any, Any], records: dict[str, Any], clock: Callable[[], float]) -> None:
    for future, (step, started, lane) in list(running.items()):
        if lane.expired:
            running.pop(future)
            # ruleid: abandon-at-lane-expiry
            records[step.name] = _left(step, [], clock() - started)


def skip_or_cut_at_lane_end(running: dict[Any, Any], records: dict[str, Any], clock: Callable[[], float]) -> None:
    for future, (step, start, lane) in list(running.items()):
        if lane.expired:
            running.pop(future)
            began = start.close()
            # ok: abandon-at-lane-expiry
            records[step.name] = StepRecord(step.name, 0.0, step.budget_s, abandoned=True, skipped=True)
            if began is not None:
                records[step.name] = StepRecord(step.name, clock() - began, step.budget_s, cut=True)


def close_at_lane_end(running: dict[Any, Any], records: dict[str, float | None]) -> None:
    for future, (step, start, lane) in list(running.items()):
        # ok: abandon-at-lane-expiry
        if lane.expired:
            running.pop(future)
            records[step.name] = start.close()
