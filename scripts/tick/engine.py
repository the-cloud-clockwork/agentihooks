"""Sequential tick engine: run a group's registered steps, each isolated and bounded by its budget."""

from collections.abc import Iterable
from contextvars import copy_context
from dataclasses import dataclass

from scripts.swarm import timing
from scripts.tick import registry
from scripts.tick.step import Context, Result, Step

__all__ = ["FAILED", "LATE", "OK", "StepRecord", "group_steps", "run_group", "run_step"]

OK = "ok"
LATE = "late"
FAILED = "failed"


@dataclass(frozen=True)
class StepRecord:
    """Outcome of one step run: its name, outcome, wall time, error and result."""

    name: str
    outcome: str
    wall_s: float
    error: str = ""
    result: Result | None = None


def group_steps(name: str, steps: Iterable[Step] | None = None) -> tuple[Step, ...]:
    """Return the steps of group ``name`` in legacy order."""
    pool = [step for wave in registry.waves() for step in wave] if steps is None else list(steps)
    return tuple(sorted((step for step in pool if step.group == name), key=lambda step: step.order))


def run_step(step: Step, ctx: Context) -> StepRecord:
    """Run one step under a copied context and its own deadline, recording failure and overrun."""
    deadline = ctx.deadline.child(step.budget_s)
    clock = deadline.clock
    started = clock()
    try:
        result = copy_context().run(timing.call, step.run, Context(ctx.view, deadline))
    except (Exception, SystemExit) as exc:
        error = f"{type(exc).__module__}.{type(exc).__qualname__}"
        return StepRecord(step.name, FAILED, clock() - started, error)
    wall = clock() - started
    return StepRecord(step.name, LATE if wall > step.budget_s else OK, wall, result=result)


def run_group(name: str, ctx: Context, steps: Iterable[Step] | None = None) -> tuple[StepRecord, ...]:
    """Run every step of group ``name`` in legacy order; a failed or late step never stops the next."""
    return tuple(run_step(step, ctx) for step in group_steps(name, steps))
