"""Sequential tick engine: run a group's registered steps, each isolated and bounded by its budget."""

import json
import os
from collections.abc import Iterable
from contextvars import copy_context
from dataclasses import dataclass
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

    @property
    def outcome(self) -> str:
        """Return failed when the step raised, late when it ran past its budget, else ok."""
        if self.error:
            return FAILED
        return LATE if self.wall_s > self.budget_s else OK


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
        result = copy_context().run(timing.within, step.budget_s, step.run, Context(ctx.view, deadline))
    except (Exception, SystemExit) as exc:
        error = f"{type(exc).__module__}.{type(exc).__qualname__}"
        return StepRecord(step.name, clock() - started, step.budget_s, error)
    return StepRecord(step.name, clock() - started, step.budget_s, result=result)


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
