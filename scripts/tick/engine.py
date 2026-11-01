"""Sequential tick engine: run a group's registered steps, each isolated and bounded by its budget."""

import json
import os
from collections.abc import Iterable
from contextvars import Context as ContextCopy
from contextvars import copy_context
from dataclasses import dataclass
from threading import Thread
from typing import Any
from uuid import uuid4

from scripts.swarm import tick_report, timing
from scripts.tick import registry
from scripts.tick.adapters.inbox import notify_pass_budget
from scripts.tick.step import Context, Result, Step

__all__ = ["FAILED", "LATE", "OK", "StepRecord", "group_steps", "run_group", "run_step"]

OK = "ok"
LATE = "late"
FAILED = "failed"


@dataclass(frozen=True)
class StepRecord:
    """Outcome of one step run: its name, wall time against its budget, error and result."""

    name: str
    wall_s: float
    budget_s: float
    error: str = ""
    result: Result | None = None
    abandoned: bool = False

    @property
    def outcome(self) -> str:
        """Return failed when the step raised, late when it ran past its budget or was left running, else ok."""
        if self.error:
            return FAILED
        return LATE if self.abandoned or self.wall_s > self.budget_s else OK


def group_steps(name: str, steps: Iterable[Step] | None = None) -> tuple[Step, ...]:
    """Return the steps of group ``name`` in legacy order."""
    pool = [step for wave in registry.waves() for step in wave] if steps is None else list(steps)
    return tuple(sorted((step for step in pool if step.group == name), key=lambda step: step.order))


def run_step(step: Step, ctx: Context) -> StepRecord:
    """Run one step under a copied context and its own deadline, waiting at most its budget for it to end.

    A step still running at its budget is recorded late and left running in its daemon thread; its result is dropped.
    The next pass can start the same step while that thread still runs.
    """
    deadline = ctx.deadline.child(step.budget_s)
    clock = deadline.clock
    started = clock()
    outcome: list[Result | BaseException] = []
    caller = copy_context()
    worker = Thread(target=_record, args=(outcome, caller, step, Context(ctx.view, deadline)), daemon=True)
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
    """Run every step of group ``name`` in legacy order; a failed or late step never stops the next."""
    return tuple(run_step(step, ctx) for step in group_steps(name, steps))


PASS_BUDGET_HOLD = "tick:budget-hold"
PASS_WINDOW = 10
STAGE_TARGETS = {"S1": 384.0, "S2": 240.0, "S3": 120.0, "S4": 60.0}


def pass_budget_hold(store: Any, slug: str) -> str:
    raw = store.redis.get(store.key(slug, PASS_BUDGET_HOLD))
    return json.loads(raw)["text"] if raw else ""


def update_pass_budget(store: Any, slug: str) -> str:
    stage = os.environ.get("AGENTIHOOKS_TICK_STAGE", "S1")
    target = STAGE_TARGETS[stage]
    rows = tick_report.history(store, slug, "tick:passes", PASS_WINDOW)
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
