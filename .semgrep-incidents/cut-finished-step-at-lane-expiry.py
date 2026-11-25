from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

from scripts.tick.engine import StepRecord


def cut_every_expired_step(
    running: dict[Future[Any], Any], records: dict[str, Any], clock: Callable[[], float]
) -> None:
    for future, (step, start, lane) in list(running.items()):
        if lane.expired:
            running.pop(future)
            began = start.close()
            # ruleid: cut-finished-step-at-lane-expiry
            records[step.name] = StepRecord(step.name, clock() - began, step.budget_s, cut=True, started_at=start.wall)


def cut_only_a_running_expired_step(
    running: dict[Future[Any], Any], records: dict[str, Any], clock: Callable[[], float]
) -> None:
    for future, (step, start, lane) in list(running.items()):
        if lane.expired and not future.done():
            running.pop(future)
            began = start.close()
            # ok: cut-finished-step-at-lane-expiry
            records[step.name] = StepRecord(step.name, clock() - began, step.budget_s, cut=True, started_at=start.wall)


def collect_a_finished_step_before_its_cut(
    running: dict[Future[Any], Any], records: dict[str, Any], clock: Callable[[], float]
) -> None:
    for future, (step, start, lane) in list(running.items()):
        if lane.expired:
            running.pop(future)
            if future.done():
                records[step.name] = future.result()
            else:
                # ok: cut-finished-step-at-lane-expiry
                records[step.name] = StepRecord(step.name, clock() - start.close(), step.budget_s, cut=True)


def cut_a_worker_that_left_no_outcome(step: Any, outcome: list[Any], clock: Callable[[], float]) -> StepRecord:
    if not outcome:
        # ok: cut-finished-step-at-lane-expiry
        return StepRecord(step.name, clock(), step.budget_s, cut=True)
    return outcome[0]
