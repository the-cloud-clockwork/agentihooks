"""Sequential tick engine: run a group's registered steps, each isolated and bounded by its budget."""

import json
import os
from collections.abc import Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextvars import Context as ContextCopy
from contextvars import copy_context
from dataclasses import dataclass
from threading import Lock, Thread
from typing import Any
from uuid import uuid4

from scripts.swarm import tick_report, timing
from scripts.tick import pool, registry
from scripts.tick.adapters.inbox import notify_pass_budget
from scripts.tick.deadline import Deadline
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
    """Return the steps of group ``name``, each after the group steps it needs, otherwise in registration order."""
    pool = registry.steps() if steps is None else tuple(steps)
    return registry.ordered(step for step in pool if step.group == name)


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
    return (_finished if outcome else _left)(step, outcome, clock() - started)


def _left(step: Step, outcome: list[Result | BaseException], wall_s: float) -> StepRecord:
    """Record a step still running at its budget as abandoned, keeping its wall time."""
    return StepRecord(step.name, wall_s, step.budget_s, abandoned=True)


def _finished(step: Step, outcome: list[Result | BaseException], wall_s: float) -> StepRecord:
    """Record a step that ended within its budget with its result, or the error it raised."""
    found = outcome[0]
    if isinstance(found, BaseException) and not isinstance(found, (Exception, SystemExit)):
        raise found
    if isinstance(found, BaseException):
        error = f"{type(found).__module__}.{type(found).__qualname__}"
        return StepRecord(step.name, wall_s, step.budget_s, error)
    return StepRecord(step.name, wall_s, step.budget_s, result=found)


def _record(outcome: list[Result | BaseException], caller: ContextCopy, step: Step, ctx: Context) -> None:
    """Run ``step`` in the caller's copied context and append its result or the error it raised."""
    try:
        outcome.append(caller.run(timing.within, step.budget_s, step.run, ctx))
    except BaseException as exc:
        outcome.append(exc)


def run_group(name: str, ctx: Context, steps: Iterable[Step] | None = None) -> tuple[StepRecord, ...]:
    """Run every step of group ``name``; a failed or late step never stops the next.

    Outside a parallel pass steps run one at a time in group order. In a parallel pass each step starts as soon as
    every step it needs has ended, records keep group order, and a lane past its deadline drops its unfinished steps
    and every step that needs them.
    """
    ordered = group_steps(name, steps)
    found = pool.active()
    if found is None or not found.parallel:
        return tuple(run_step(step, ctx) for step in ordered)
    return _eager(ordered, ctx, found)


def _lane_context(step: Step, ctx: Context, found: pool.Pass) -> Context:
    """Return ``ctx`` with its deadline capped by the deadline of the step's lane."""
    lane = found.lane(step.lane)
    clock = ctx.deadline.clock
    return Context(ctx.view, Deadline(clock() + min(ctx.deadline.remaining, lane.remaining), clock))


def _eager(ordered: tuple[Step, ...], ctx: Context, found: pool.Pass) -> tuple[StepRecord, ...]:
    """Run ``ordered`` on the pass workers, each step submitted the moment its needs have ended."""
    clock = ctx.deadline.clock
    names = {step.name for step in ordered}
    records: dict[str, StepRecord] = {}
    dropped: set[str] = set()
    pending = list(ordered)
    running: dict[Future[StepRecord], tuple[Step, float, Deadline]] = {}
    executor = ThreadPoolExecutor(max_workers=found.workers)
    try:
        while pending or running:
            ready = [step for step in pending if step.needs & names <= records.keys()]
            for step in ready:
                pending.remove(step)
                lane = found.lane(step.lane)
                if step.needs & dropped or lane.expired:
                    dropped.add(step.name)
                    records[step.name] = StepRecord(step.name, 0.0, step.budget_s, abandoned=True)
                else:
                    future = executor.submit(copy_context().run, run_step, step, _lane_context(step, ctx, found))
                    running[future] = (step, clock(), lane)
            if not running:
                if not ready:
                    raise ValueError(f"steps {sorted(step.name for step in pending)} wait on needs that never end")
                continue
            timeout = min(lane.remaining for _, _, lane in running.values())
            done, _ = wait(running, timeout=timeout, return_when=FIRST_COMPLETED)
            for future in done:
                step, _, _ = running.pop(future)
                records[step.name] = future.result()
            for future, (step, started, lane) in list(running.items()):
                if lane.expired:
                    running.pop(future)
                    dropped.add(step.name)
                    records[step.name] = StepRecord(step.name, clock() - started, step.budget_s, abandoned=True)
    finally:
        executor.shutdown(False, cancel_futures=True)
    return tuple(records[step.name] for step in ordered)


PASS_BUDGET_HOLD = "tick:budget-hold"
PASS_WINDOW = 10
STAGE_TARGETS = {"S1": 384.0, "S2": 240.0, "S3": 120.0, "S4": 60.0}


def pass_budget_hold(store: Any, slug: str) -> str:
    raw = store.redis.get(store.key(slug, PASS_BUDGET_HOLD))
    return json.loads(raw)["text"] if raw else ""


def update_pass_budget(store: Any, slug: str) -> str:
    from redis.exceptions import RedisError

    stage = os.environ.get("AGENTIHOOKS_TICK_STAGE", "S1")
    target = STAGE_TARGETS[stage]
    try:
        rows = tick_report.history(store, slug, "tick:passes", PASS_WINDOW)
    except RedisError:
        rows = []
    if len(rows) < PASS_WINDOW:
        return pass_budget_hold(store, slug)
    measured = tick_report.p95([row["wall_s"] for row in rows])
    key = store.key(slug, PASS_BUDGET_HOLD)
    if measured is not None and measured > target:
        text = f"holding spawns: pass p95 {measured:g}s exceeds {stage} target {target:g}s"
        store.redis.set(key, json.dumps({"text": text, "notice": uuid4().hex, "notified": False}), nx=True)
        held = json.loads(store.redis.get(key))
        if not held["notified"]:
            notify_pass_budget(store.redis, slug, held["text"], held["notice"])
            held["notified"] = True
            store.redis.set(key, json.dumps(held))
        return held["text"]
    store.redis.delete(key)
    return ""
