"""Sequential tick engine: run a group's registered steps, each isolated and bounded by its budget."""

from collections.abc import Iterable
from contextvars import Context as ContextCopy
from contextvars import copy_context
from dataclasses import dataclass
from threading import Lock, Thread

from scripts.swarm import timing
from scripts.tick import registry
from scripts.tick.step import Context, Result, Step

__all__ = ["FAILED", "HELD", "LATE", "OK", "StepRecord", "group_steps", "run_group", "run_step"]

OK = "ok"
LATE = "late"
FAILED = "failed"
HELD = "held"

_RUNNING: dict[str, Thread] = {}
_RUNNING_LOCK = Lock()


@dataclass(frozen=True)
class StepRecord:
    """Outcome of one step run: its name, wall time against its budget, error and result."""

    name: str
    wall_s: float
    budget_s: float
    error: str = ""
    result: Result | None = None
    abandoned: bool = False
    held: bool = False

    @property
    def outcome(self) -> str:
        """Return failed when the step raised, held when its earlier run was alive, late when it ran past its budget or was left running, else ok."""
        if self.error:
            return FAILED
        if self.held:
            return HELD
        return LATE if self.abandoned or self.wall_s > self.budget_s else OK


def group_steps(name: str, steps: Iterable[Step] | None = None) -> tuple[Step, ...]:
    """Return the steps of group ``name`` in legacy order."""
    pool = [step for wave in registry.waves() for step in wave] if steps is None else list(steps)
    return tuple(sorted((step for step in pool if step.group == name), key=lambda step: step.order))


def run_step(step: Step, ctx: Context) -> StepRecord:
    """Run one step under a copied context and its own deadline, waiting at most its budget for it to end.

    A step still running at its budget is recorded late and left running in its daemon thread; its result is dropped.
    While that earlier run is alive, a new run of the same step is held: no thread starts and the record reads held.
    """
    deadline = ctx.deadline.child(step.budget_s)
    clock = deadline.clock
    started = clock()
    outcome: list[Result | BaseException] = []
    caller = copy_context()
    with _RUNNING_LOCK:
        earlier = _RUNNING.get(step.name)
        if earlier is not None and earlier.is_alive():
            return StepRecord(step.name, 0.0, step.budget_s, held=True)
        worker = Thread(target=_record, args=(outcome, caller, step, Context(ctx.view, deadline)), daemon=True)
        _RUNNING[step.name] = worker
        worker.start()
    worker.join(step.budget_s)
    if not outcome:
        return StepRecord(step.name, clock() - started, step.budget_s, abandoned=True)
    found = outcome[0]
    if isinstance(found, BaseException) and not isinstance(found, (Exception, SystemExit)):
        raise found
    if isinstance(found, BaseException):
        error = f"{type(found).__module__}.{type(found).__qualname__}"
        return StepRecord(step.name, clock() - started, step.budget_s, error)
    return StepRecord(step.name, clock() - started, step.budget_s, result=found)


def _record(outcome: list[Result | BaseException], caller: ContextCopy, step: Step, ctx: Context) -> None:
    """Run ``step`` in the caller's copied context and append its result or the error it raised."""
    try:
        outcome.append(caller.run(timing.within, step.budget_s, step.run, ctx))
    except BaseException as exc:
        outcome.append(exc)


def run_group(name: str, ctx: Context, steps: Iterable[Step] | None = None) -> tuple[StepRecord, ...]:
    """Run every step of group ``name`` in legacy order; a failed or late step never stops the next."""
    return tuple(run_step(step, ctx) for step in group_steps(name, steps))
