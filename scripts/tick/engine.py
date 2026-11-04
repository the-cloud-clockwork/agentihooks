"""Sequential tick engine: run a group's registered steps, each isolated and bounded by its budget."""

import json
import os
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextvars import Context as ContextCopy
from contextvars import copy_context
from dataclasses import dataclass, replace
from functools import partial
from threading import Lock, Thread
from typing import Any
from uuid import uuid4

from scripts.swarm import tick_report, timing
from scripts.tick import pool, registry
from scripts.tick.adapters.inbox import notify_pass_budget
from scripts.tick.deadline import Deadline
from scripts.tick.ports.records import RevisionConflict
from scripts.tick.step import Context, Domain, Lane, Result, Step, Write

__all__ = [
    "FAILED",
    "HELD",
    "LATE",
    "OK",
    "Appliers",
    "StepRecord",
    "cadence_breaches",
    "cadence_s",
    "group_steps",
    "interval_s",
    "run_group",
    "run_step",
]

Appliers = Mapping[Domain, Callable[[Write], list[str] | None]]

OK = "ok"
LATE = "late"
FAILED = "failed"
HELD = "held"
SKIPPED = "skipped"

_RUNNING: dict[tuple[str | None, str], Thread] = {}
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
    skipped: bool = False
    dropped: tuple[Write, ...] = ()
    conflicts: tuple[str, ...] = ()
    applied: tuple[str, ...] = ()

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
    While that earlier run is alive, a new run of the same step for the same swarm is held: no thread starts and the
    record reads held. Another swarm's run of that step is never held by it.
    """
    deadline = ctx.deadline.child(step.budget_s)
    clock = deadline.clock
    started = clock()
    outcome: list[Result | BaseException] = []
    caller = copy_context()
    key = (timing.SWARM.get(), step.name)
    with _RUNNING_LOCK:
        earlier = _RUNNING.get(key)
        if earlier is not None and earlier.is_alive():
            return StepRecord(step.name, 0.0, step.budget_s, held=True)
        worker = Thread(target=_record, args=(outcome, caller, step, Context(ctx.view, deadline)), daemon=True)
        _RUNNING[key] = worker
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
    """Run ``step`` in the caller's copied context, recorded under its registered name, and append its result or the
    error it raised."""
    try:
        outcome.append(caller.run(timing.within, step.budget_s, step.run, ctx, lane=step.lane, name=step.name))
    except BaseException as exc:
        outcome.append(exc)


def run_group(
    name: str,
    ctx: Context,
    steps: Iterable[Step] | None = None,
    apply: Appliers | None = None,
    finished: Callable[[tuple[StepRecord, ...]], object] | None = None,
) -> tuple[StepRecord, ...]:
    """Run every step of group ``name``, applying each step's writes through ``apply`` as it ends.

    A failed or late step never stops the next. Outside a parallel pass steps run one at a time in group order. In a
    parallel pass each step starts as soon as every step it needs has ended, records keep group order, writes are
    applied on the scheduling thread as each step ends, and a lane past its deadline drops its unfinished steps and
    every step that needs them. Slow lane steps, and the steps that need them, start once the group's other steps
    end and run on after the group returns: the pass joins them when it ends and hands their records to ``finished``.
    A step that needs a step an earlier group of the pass handed off starts once that step ended or was held, handed
    off too while that step still runs; it is dropped when that step was dropped or left running past its budget or
    its lane.
    """
    ordered = group_steps(name, steps)
    found = pool.active()
    if found is None or not found.parallel:
        return tuple(_sequential(step, ctx, apply or {}) for step in ordered)
    later = _slow_lane(ordered, found.pending())
    names = {step.name for step in later}
    records = _eager(tuple(step for step in ordered if step.name not in names), ctx, found, apply or {})
    if later:
        found.hand(names)
        gone = _gone(later, frozenset(record.name for record in records if record.abandoned))
        for each in gone:
            found.end(each, True)
        found.detach(partial(_run_later, later, gone, ctx, found, apply or {}, finished))
    return records


def _slow_lane(ordered: tuple[Step, ...], handed: frozenset[str]) -> tuple[Step, ...]:
    """Return the slow lane steps of ``ordered`` and every step that needs one of them or a still running ``handed``
    step of an earlier group, in group order."""
    names: set[str] = set(handed)
    for step in ordered:
        if step.lane == SLOW or step.needs & names:
            names.add(step.name)
    return tuple(step for step in ordered if step.name in names)


def _gone(later: tuple[Step, ...], dropped: frozenset[str]) -> frozenset[str]:
    """Return ``dropped`` with every step of ``later`` that needs one of them, directly or through another."""
    gone = set(dropped)
    for step in later:
        if step.needs & gone:
            gone.add(step.name)
    return frozenset(gone)


def _run_later(
    later: tuple[Step, ...],
    gone: frozenset[str],
    ctx: Context,
    found: pool.Pass,
    apply: Appliers,
    finished: Callable[[tuple[StepRecord, ...]], object] | None,
) -> None:
    """Run the handed off steps on the pass workers, dropping each in ``gone``, then hand their records in group
    order to ``finished``."""
    ran = {record.name: record for record in _eager(tuple(s for s in later if s.name not in gone), ctx, found, apply)}
    records = tuple(ran.get(step.name) or _skipped(step, _offset()) for step in later)
    if finished is not None:
        finished(records)


def _sequential(step: Step, ctx: Context, apply: Appliers) -> StepRecord:
    """Run ``step`` alone, apply its writes and journal its record with its start offset in the pass."""
    offset = _offset()
    return _journal(step, _applied(run_step(step, ctx), apply), offset)


def _skipped(step: Step, offset: float | None) -> StepRecord:
    """Return and journal the record of ``step`` dropped before it started."""
    return _journal(step, StepRecord(step.name, 0.0, step.budget_s, abandoned=True, skipped=True), offset)


def _offset() -> float | None:
    """Return the seconds since the recording pass started, or None outside a recording pass."""
    current = timing.PASS.get()
    return None if current is None else time.time() - current["start_ms"] / 1000


def _critical(step: Step) -> bool:
    """Return whether ``step`` is on the pass critical chain: outside the slow lane and not handed off by its group."""
    found = pool.active()
    return step.lane != SLOW and not (found is not None and found.handed_of((step.name,)))


def _journal(step: Step, record: StepRecord, offset: float | None) -> StepRecord:
    """Append one ``tick:records`` row for ``record`` while a pass is recording, and return the record unchanged.

    A step on the critical chain moves the pass record's ``critical_end_s`` to its end offset when that is later. An
    abandoned step that started counts every write domain it declares as dropped, since its result never applies.
    """
    store, slug, current = timing.STORE.get(), timing.SWARM.get(), timing.PASS.get()
    if store is None or slug is None or current is None:
        return record
    if record.result is not None:
        dropped = len(record.dropped)
    else:
        dropped = len(step.writes) if record.abandoned and not record.skipped else 0
    row = {
        "pass_id": current["pass_id"],
        "step": record.name,
        "lane": step.lane,
        "outcome": SKIPPED if record.skipped else record.outcome,
        "wall_s": record.wall_s,
        "budget_s": record.budget_s,
        "start_s": offset,
        "abandoned": record.abandoned and not record.skipped,
        "held": record.held,
        "skipped": record.skipped,
        "dropped": dropped,
        "conflicts": len(record.conflicts),
    }
    if offset is not None and _critical(step):
        current["critical_end_s"] = max(current.get("critical_end_s", 0.0), offset + record.wall_s)
    timing.append_record(store, slug, "tick:records", row)
    return record


def _applied(record: StepRecord, apply: Appliers) -> StepRecord:
    """Apply the record's writes through the applier of their domain, dropping each one whose compare and set failed.

    Writes for a domain with no applier pass through untouched. A dropped write is recomputed by the next pass. The
    action lines an applier returns are kept on the record as its applied actions.
    """
    if record.result is None:
        return record
    dropped: list[Write] = []
    conflicts: list[str] = []
    lines: list[str] = []
    for write in record.result.writes:
        if (applier := apply.get(write.domain)) is None:
            continue
        try:
            found = applier(write)
        except RevisionConflict as exc:
            dropped.append(write)
            conflicts.append(str(exc))
            continue
        lines += found or []
    if not dropped and not lines:
        return record
    return replace(record, dropped=tuple(dropped), conflicts=tuple(conflicts), applied=tuple(lines))


def _lane_context(step: Step, ctx: Context, found: pool.Pass) -> Context:
    """Return ``ctx`` with its deadline capped by the deadline of the step's lane."""
    lane = found.lane(step.lane)
    clock = ctx.deadline.clock
    return Context(ctx.view, Deadline(clock() + min(ctx.deadline.remaining, lane.remaining), clock))


def _after(earlier: frozenset[str], step: Step, ctx: Context, found: pool.Pass) -> StepRecord:
    """Run ``step`` once every handed off step of an earlier group in ``earlier`` has ended or was held, dropping it
    when one of them was dropped or its lane ran out first."""
    lane = found.lane(step.lane)
    if earlier and (not found.ended(earlier, lane.remaining) or lane.expired):
        return StepRecord(step.name, 0.0, step.budget_s, abandoned=True, skipped=True)
    return run_step(step, ctx)


def _ended(found: pool.Pass, record: StepRecord) -> StepRecord:
    """Mark the record's step ended on the pass, dropped when it was abandoned, and return the record."""
    found.end(record.name, record.abandoned)
    return record


def _eager(ordered: tuple[Step, ...], ctx: Context, found: pool.Pass, apply: Appliers) -> tuple[StepRecord, ...]:
    """Run ``ordered`` on the pass workers, each step submitted the moment its needs have ended.

    Each finished step's writes are applied through ``apply`` on this scheduling thread before its dependents start.
    """
    clock = ctx.deadline.clock
    names = {step.name for step in ordered}
    records: dict[str, StepRecord] = {}
    dropped: set[str] = set()
    pending = list(ordered)
    running: dict[Future[StepRecord], tuple[Step, float, Deadline]] = {}
    offsets: dict[str, float | None] = {}
    executor = ThreadPoolExecutor(max_workers=found.workers)
    try:
        while pending or running:
            ready = [step for step in pending if step.needs & names <= records.keys()]
            for step in ready:
                pending.remove(step)
                lane = found.lane(step.lane)
                offsets[step.name] = _offset()
                if step.needs & dropped or lane.expired:
                    dropped.add(step.name)
                    records[step.name] = _ended(
                        found, StepRecord(step.name, 0.0, step.budget_s, abandoned=True, skipped=True)
                    )
                else:
                    earlier = found.handed_of(step.needs - names)
                    lane_ctx = _lane_context(step, ctx, found)
                    future = executor.submit(copy_context().run, _after, earlier, step, lane_ctx, found)
                    running[future] = (step, clock(), lane)
            if not running:
                if not ready:
                    raise ValueError(f"steps {sorted(step.name for step in pending)} wait on needs that never end")
                continue
            timeout = min(lane.remaining for _, _, lane in running.values())
            done, _ = wait(running, timeout=timeout, return_when=FIRST_COMPLETED)
            for future in done:
                step, _, _ = running.pop(future)
                records[step.name] = _ended(found, _applied(future.result(), apply))
            for future, (step, started, lane) in list(running.items()):
                if lane.expired:
                    running.pop(future)
                    dropped.add(step.name)
                    left = StepRecord(step.name, clock() - started, step.budget_s, abandoned=True)
                    records[step.name] = _ended(found, left)
    finally:
        executor.shutdown(False, cancel_futures=True)
    return tuple(_journal(step, records[step.name], offsets[step.name]) for step in ordered)


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


CADENCE_ENV = "AGENTIHOOKS_TICK_CADENCE"
CRON_S = 60.0
SLOW = Lane("slow")


def cadence_breaches(cadence: float) -> list[str]:
    """Return each guard a slow lane pass every ``cadence`` seconds breaks; every guard outlasts one missed pass."""
    from scripts.swarm import lease, scheduled_tick, tick_heartbeat, tick_watch

    missed_s = 2 * cadence
    checks = {
        "delivery guard": scheduled_tick.FRESH_S >= missed_s,
        "heartbeat limit": tick_heartbeat.LIMIT_MS / 1000 >= missed_s,
        "lease TTL": lease.ttl_ms() / 1000 >= 2 * CRON_S,
        "stale watcher": tick_watch.PASS_S + tick_watch.SLUG_PASS_S <= tick_heartbeat.LIMIT_MS / 1000 - cadence,
    }
    return [name for name, held in checks.items() if not held]


def cadence_s(environ: Mapping[str, str] = os.environ) -> float:
    """Return the slow lane cadence in seconds from whole minutes in the setting, one minute when it is unset."""
    raw = environ.get(CADENCE_ENV, "").strip() or "1"
    try:
        minutes = int(raw)
    except ValueError as exc:
        raise ValueError(f"{CADENCE_ENV} must be whole minutes, got {raw!r}") from exc
    if minutes < 1:
        raise ValueError(f"{CADENCE_ENV} must be at least 1 minute, got {minutes}")
    breached = cadence_breaches(minutes * CRON_S)
    if breached:
        raise ValueError(f"{CADENCE_ENV} of {minutes} minutes breaks the {', '.join(breached)}")
    return minutes * CRON_S


def interval_s(step: Step, cadence: float) -> float:
    """Return the interval ``step`` runs at: a slow lane step at least every ``cadence`` seconds, others unchanged."""
    return max(step.interval_s, cadence) if step.lane == SLOW else step.interval_s
