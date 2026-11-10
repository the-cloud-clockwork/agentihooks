"""Sequential tick engine: run a group's registered steps, each isolated and bounded by its budget."""

import json
import os
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, wait
from contextvars import Context as ContextCopy
from contextvars import copy_context
from dataclasses import dataclass, field, replace
from functools import partial
from threading import Event, Lock, Thread
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
    "Gate",
    "SiteGate",
    "StepRecord",
    "cadence_breaches",
    "cadence_s",
    "group_steps",
    "interval_s",
    "resting",
    "run_graph",
    "run_group",
    "run_step",
]

Appliers = Mapping[Domain, Callable[[Write], list[str] | None]]
Gate = Callable[[Step, Mapping[str, "StepRecord"]], bool]

OK = "ok"
LATE = "late"
FAILED = "failed"
HELD = "held"
SKIPPED = "skipped"
CUT = "cut"
CUT_EVENT = "swarm_tick_step_cut"
END_GRACE_S = 1.0
SETTLE_S = 0.05

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
    rested: bool = False
    cut: bool = False

    @property
    def outcome(self) -> str:
        """Return skipped when its interval had not elapsed, failed when the step raised, held when its earlier run was
        alive, cut when its lane ended while it ran inside its budget, late when it ran past its budget or was left
        running at it, else ok."""
        if self.rested:
            return SKIPPED
        if self.error:
            return FAILED
        if self.held:
            return HELD
        if self.cut:
            return CUT
        return LATE if self.abandoned or self.wall_s > self.budget_s else OK

    @property
    def lost(self) -> bool:
        """Return whether the pass stopped waiting on the step before it ended: abandoned, skipped or cut."""
        return self.abandoned or self.cut


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
        tools = None if ctx.tools is None else ctx.tools.for_effects(step.effects)
        run = _Run()
        worker = Thread(
            target=_record, args=(outcome, caller, step, Context(ctx.view, deadline, tools), run), daemon=True
        )
        _RUNNING[key] = worker
        worker.start()
    worker.join(step.budget_s)
    if run.give_up():
        return _left(step, outcome, clock() - started)
    worker.join(END_GRACE_S)
    if not outcome:
        _lost_writes(step, CUT_EVENT)
        return StepRecord(step.name, clock() - started, step.budget_s, cut=True)
    return _finished(step, outcome, clock() - started)


@dataclass
class _Run:
    """One step run's end, decided once under a lock: it ended inside its budget, or was given up at it."""

    lock: Lock = field(default_factory=Lock)
    ended: bool = False
    given_up: bool = False

    def end(self) -> bool:
        """Mark the run ended and return whether it had already been given up at its budget."""
        with self.lock:
            self.ended = True
            return self.given_up

    def give_up(self) -> bool:
        """Give the run up at its budget unless it already ended, and return whether it was given up."""
        with self.lock:
            self.given_up = not self.ended
            return self.given_up


@dataclass
class _Start:
    """When one scheduled step's run began, decided once under a lock against the end of its lane."""

    clock: Callable[[], float]
    lock: Lock = field(default_factory=Lock)
    at: float | None = None
    closed: bool = False

    def begin(self) -> bool:
        """Record that the run starts now and return True, or return False when its lane end already closed it."""
        with self.lock:
            if self.closed:
                return False
            self.at = self.clock()
            return True

    def close(self) -> float | None:
        """Close the step to any later start and return when its run began, or None when it never began."""
        with self.lock:
            self.closed = True
            return self.at


def _left(step: Step, outcome: list[Result | BaseException], wall_s: float) -> StepRecord:
    """Record a step still running at its budget as abandoned, keeping its wall time, and journal its lost writes."""
    _abandoned(step)
    return StepRecord(step.name, wall_s, step.budget_s, abandoned=True)


def _abandoned(step: Step) -> None:
    """Journal one ``swarm_tick_step_abandoned`` line for ``step`` when it declares writes beyond its own actions."""
    _lost_writes(step, "swarm_tick_step_abandoned")


def _lost_writes(step: Step, event: str) -> None:
    """Journal one ``event`` line naming the write domains of ``step`` beyond its own actions, when it has any."""
    writes = sorted(step.writes - {Domain(f"actions:{step.name}")})
    if writes:
        line = {"event": event, "slug": timing.SWARM.get(), "step": step.name}
        print(json.dumps({**line, "budget_s": step.budget_s, "writes": writes}), file=sys.stderr, flush=True)


def _finished(step: Step, outcome: list[Result | BaseException], wall_s: float) -> StepRecord:
    """Record a step that ended within its budget with its result, or the error it raised."""
    found = outcome[0]
    if isinstance(found, BaseException) and not isinstance(found, (Exception, SystemExit)):
        raise found
    if isinstance(found, BaseException):
        error = f"{type(found).__module__}.{type(found).__qualname__}"
        return StepRecord(step.name, wall_s, step.budget_s, error)
    return StepRecord(step.name, wall_s, step.budget_s, result=found)


def _record(outcome: list[Result | BaseException], caller: ContextCopy, step: Step, ctx: Context, run: _Run) -> None:
    """Run ``step`` in the caller's copied context, recorded under its registered name, and append its result or the
    error it raised; the run reads abandoned when ``run`` was given up at its budget before it ended."""
    try:
        outcome.append(
            caller.run(timing.within, step.budget_s, step.run, ctx, lane=step.lane, name=step.name, on_end=run.end)
        )
    except BaseException as exc:
        outcome.append(exc)
    finally:
        run.end()


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
    every step that needs them. Slow lane steps, and the steps that need them, run in their own schedule started
    with the group, each once the steps it needs have ended, and run on after the group returns: the pass joins them
    when it ends and hands their records to ``finished``.
    A step that needs a step an earlier group of the pass handed off starts once that step ended or was held, handed
    off too while that step still runs; it is dropped when that step was dropped or left running past its budget or
    its lane.
    """
    return _scheduled(group_steps(name, steps), ctx, apply, finished)


def run_graph(
    steps: Iterable[Step],
    ctx: Context,
    apply: Appliers | None = None,
    finished: Callable[[tuple[StepRecord, ...]], object] | None = None,
    gate: Gate | None = None,
) -> tuple[StepRecord, ...]:
    """Run every step of ``steps`` as one graph, each after the steps among them it needs, scheduled as
    ``run_group`` schedules one group: one at a time outside a parallel pass, eagerly on the pass workers inside one,
    slow lane steps and the steps that need them handed to ``finished``. A step ``gate`` shuts as it becomes ready
    rests: it starts no thread and ends at once for the steps that need it."""
    return _scheduled(registry.ordered(steps), ctx, apply, finished, gate)


def _scheduled(
    ordered: tuple[Step, ...],
    ctx: Context,
    apply: Appliers | None,
    finished: Callable[[tuple[StepRecord, ...]], object] | None,
    gate: Gate | None = None,
) -> tuple[StepRecord, ...]:
    """Run ``ordered`` in one schedule, applying each step's writes through ``apply`` as it ends and resting each
    step ``gate`` shuts as it becomes ready."""
    rest = resting(ordered, cadence_s())
    found = pool.active()
    if found is None or not found.parallel:
        return _in_turn(ordered, ctx, apply or {}, rest, gate)
    later = _slow_lane(ordered, found.pending())
    names = {step.name for step in later}
    first = tuple(step for step in ordered if step.name not in names)
    if later:
        awaited = frozenset[str]().union(*(step.needs for step in later)) & {step.name for step in first}
        found.hand(names)
        found.track(awaited)
        opened = partial(_opened, rest, gate)
        found.detach(partial(_run_later, later, awaited, ctx, found, apply or {}, finished, opened))
    records = _eager(first, ctx, found, apply or {}, rest, gate)
    for each in _gone(later, frozenset(record.name for record in records if record.lost)):
        found.end(each, True)
    return records


def _gone(later: tuple[Step, ...], dropped: frozenset[str]) -> frozenset[str]:
    """Return the steps of ``later`` that need one of ``dropped``, directly or through another, so a later group sees
    them ended and dropped as soon as their group returns."""
    gone = set(dropped)
    for step in later:
        if step.needs & gone:
            gone.add(step.name)
    return frozenset(gone - dropped)


def _in_turn(
    ordered: tuple[Step, ...], ctx: Context, apply: Appliers, rest: frozenset[str], gate: Gate | None
) -> tuple[StepRecord, ...]:
    """Run ``ordered`` one step at a time, resting each step in ``rest`` or shut by ``gate``."""
    records: dict[str, StepRecord] = {}
    for step in ordered:
        records[step.name] = _sequential(step, ctx, apply, rest | _shut(gate, step, records))
    return tuple(records.values())


def _shut(gate: Gate | None, step: Step, records: Mapping[str, StepRecord]) -> frozenset[str]:
    """Return the name of ``step`` when ``gate`` shuts it now that it is ready, else nothing."""
    return frozenset() if gate is None or gate(step, records) else frozenset({step.name})


@dataclass
class SiteGate:
    """Open or shut each step of a pass graph as it becomes ready.

    A site's hook runs once, when the site's first step is ready, and shuts the whole site when it answers false;
    ``shut`` shuts single steps and is asked before and after the hook. ``seen`` holds the records of the steps that
    ended, on either thread, before the latest step became ready. The handed lane and the rest of the graph ask it
    from their own threads; a site's hook runs once, outside the guard.
    """

    hooks: Mapping[str, Callable[[], bool]]
    shut: Callable[[Step], bool]
    seen: dict[str, StepRecord] = field(default_factory=dict[str, StepRecord])
    opened: dict[str, bool] = field(default_factory=dict[str, bool])
    guard: Lock = field(default_factory=Lock, compare=False)
    opening: dict[str, Event] = field(default_factory=dict[str, Event], compare=False)

    def __call__(self, step: Step, records: Mapping[str, StepRecord]) -> bool:
        """Return whether ``step`` runs, running its site's hook first when it is the site's first ready step; a
        step of a site whose hook runs on another thread of the pass waits for it at most its own budget, never past
        its lane, and stays shut when the hook did not answer by then; steps of other sites never wait, and outside a
        pass the hook has always answered before the next step asks."""
        with self.guard:
            self.seen.update(records)
            if self.shut(step):
                return False
            first = step.group not in self.opening
            opening = self.opening.setdefault(step.group, Event())
        if first:
            self._open(step.group, opening)
        found = pool.active()
        if found is not None and not opening.wait(min(step.budget_s, found.lane(step.lane).remaining)):
            return False
        with self.guard:
            return bool(self.opened.get(step.group)) and not self.shut(step)

    def _open(self, group: str, opening: Event) -> None:
        """Run the hook of site ``group`` and keep its answer, then release the steps waiting on it."""
        try:
            hook = self.hooks.get(group)
            self.opened[group] = hook is None or hook()
        finally:
            opening.set()


def _slow_lane(ordered: tuple[Step, ...], handed: frozenset[str]) -> tuple[Step, ...]:
    """Return the slow lane steps of ``ordered`` and every step that needs one of them or a still running ``handed``
    step of an earlier group, in group order."""
    names: set[str] = set(handed)
    for step in ordered:
        if step.lane == SLOW or step.needs & names:
            names.add(step.name)
    return tuple(step for step in ordered if step.name in names)


def _run_later(
    later: tuple[Step, ...],
    awaited: frozenset[str],
    ctx: Context,
    found: pool.Pass,
    apply: Appliers,
    finished: Callable[[tuple[StepRecord, ...]], object] | None,
    gate: Gate,
) -> None:
    """Run the handed off steps on the pass workers alongside the rest of their graph, each once the steps it needs
    ended, those in ``awaited`` included, dropping each whose need was dropped and resting each ``gate`` shuts, then
    hand their records in group order to ``finished``."""
    records = _eager(later, ctx, found, apply, frozenset(), gate, awaited)
    if finished is not None:
        finished(records)


def _opened(rest: frozenset[str], gate: Gate | None, step: Step, records: Mapping[str, StepRecord]) -> bool:
    """Return whether ``step`` runs: ``gate`` opens it, asked first, and its interval has elapsed."""
    opened = gate is None or gate(step, records)
    return opened and step.name not in rest


def _sequential(step: Step, ctx: Context, apply: Appliers, rest: frozenset[str]) -> StepRecord:
    """Run ``step`` alone, or skip it when it is in ``rest``, apply its writes and journal its record with its start
    offset in the pass."""
    offset = _offset()
    if step.name in rest:
        return _journal(step, _rested(step), offset)
    return _journal(step, _applied(run_step(step, ctx), apply), offset)


def _rested(step: Step) -> StepRecord:
    """Return the record of ``step`` skipped because its interval had not elapsed: no thread, ended for dependents."""
    return StepRecord(step.name, 0.0, step.budget_s, rested=True)


def resting(ordered: Iterable[Step], cadence: float) -> frozenset[str]:
    """Return the steps of ``ordered`` whose interval at ``cadence`` has not elapsed since their last ended run.

    Only a step whose interval is above one cron minute can rest. A step rests while the pass start, or the clock
    outside a recording pass, is within its interval less one cron minute of its last end, so a step runs again on
    the first pass starting more than that after its last end, up to one cron minute before its full interval has
    passed since that end. Without a recording store nothing rests.
    """
    store, slug, current = timing.STORE.get(), timing.SWARM.get(), timing.PASS.get()
    every = {step.name: interval_s(step, cadence) for step in ordered}
    wide = {name: seconds for name, seconds in every.items() if seconds > CRON_S}
    if store is None or slug is None or not wide:
        return frozenset()
    now = time.time() if current is None else current["start_ms"] / 1000
    ends = timing.step_ends(store, slug)
    return frozenset(name for name, every in wide.items() if name in ends and now - ends[name] <= every - CRON_S)


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
        dropped = len(step.writes) if record.lost and not record.skipped else 0
    row = {
        "pass_id": current["pass_id"],
        "step": record.name,
        "lane": step.lane,
        "outcome": SKIPPED if record.skipped else record.outcome,
        "wall_s": record.wall_s,
        "budget_s": record.budget_s,
        "start_s": offset,
        "abandoned": record.abandoned and not record.skipped,
        "cut": record.cut,
        "held": record.held,
        "skipped": record.skipped or record.rested,
        "rested": record.rested,
        "dropped": dropped,
        "conflicts": len(record.conflicts),
    }
    if record.outcome in (OK, FAILED) and (step.interval_s > CRON_S or step.lane == SLOW):
        timing.step_ended(store, slug, step.name, time.time())
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


def _held_back(ordered: tuple[Step, ...], reserves: Mapping[str, float]) -> dict[str, float]:
    """Return the seconds at the end of its lane each step of ``ordered`` leaves to the reserved steps that need it,
    directly or through another: the largest reserve among them."""
    held: dict[str, float] = {}
    for step in reversed(ordered):
        give = max(held.get(step.name, 0.0), reserves.get(step.name, 0.0))
        for need in step.needs:
            held[need] = max(held.get(need, 0.0), give)
    return held


def _lane_of(step: Step, found: pool.Pass, held: Mapping[str, float]) -> Deadline:
    """Return the deadline of the step's lane less the seconds it leaves to the reserved steps that need it."""
    lane = found.lane(step.lane)
    return Deadline(lane.expires_at - held.get(step.name, 0.0), lane.clock)


def _drops(step: Step, dropped: set[str], found: pool.Pass) -> bool:
    """Return whether ``step``, cut before it ended, drops its dependents: a need was dropped or its whole lane ran out,
    never a reserve it left to a step that needs it."""
    return bool(step.needs & dropped) or found.lane(step.lane).expired


def _cut_off(
    step: Step, lane: Deadline, lost: tuple[set[str], set[str]], found: pool.Pass, held: Mapping[str, float]
) -> set[str] | None:
    """Return the set of ``lost``, dropped or cut, that ``step`` joins when it cannot start, else None.

    A step whose need was dropped or whose whole lane ran out is dropped. A step whose lane less its reserve ran out,
    or that needs a cut step, is cut when a reserved step needs it and dropped otherwise; a reserved step runs on
    after its cut needs.
    """
    dropped, cut = lost
    if _drops(step, dropped, found):
        return dropped
    if lane.expired or (step.needs & cut and step.name not in found.reserves):
        return cut if step.name in held else dropped
    return None


def _lane_context(lane: Deadline, ctx: Context) -> Context:
    """Return ``ctx`` with its deadline capped by ``lane``."""
    clock = ctx.deadline.clock
    return Context(ctx.view, Deadline(clock() + min(ctx.deadline.remaining, lane.remaining), clock), ctx.tools)


def _after(earlier: frozenset[str], step: Step, ctx: Context, found: pool.Pass, start: _Start) -> StepRecord:
    """Run ``step`` once every handed off step of an earlier group in ``earlier`` has ended or was held, dropping it
    when one of them was dropped or its lane ran out before it started."""
    lane = found.lane(step.lane)
    if earlier and not found.ended(earlier, lane.remaining):
        return StepRecord(step.name, 0.0, step.budget_s, abandoned=True, skipped=True)
    if lane.expired or not start.begin():
        return StepRecord(step.name, 0.0, step.budget_s, abandoned=True, skipped=True)
    return run_step(step, ctx)


def _earlier(step: Step, names: set[str], found: pool.Pass, awaited: frozenset[str]) -> frozenset[str]:
    """Return the steps outside ``names`` that ``step`` waits on: those an earlier group handed off and those of its
    own graph in ``awaited``."""
    return found.handed_of(step.needs - names) | (step.needs & awaited)


def _waiting(step: Step, names: set[str], found: pool.Pass, awaited: frozenset[str] = frozenset()) -> bool:
    """Return whether ``step`` still waits, inside its lane, on a step outside ``names`` it waits on.

    Such a step is submitted only once that step ended, so it never holds a pass worker while it waits.
    """
    earlier = _earlier(step, names, found, awaited)
    return bool(earlier) and not found.lane(step.lane).expired and not found.settled(earlier)


def _ended(found: pool.Pass, record: StepRecord) -> StepRecord:
    """Mark the record's step ended on the pass, dropped when it was abandoned, and return the record."""
    found.end(record.name, record.lost)
    return record


def _kept(step: Step) -> bool:
    """Return whether ``step`` is kept: it runs under its own budget and the pass deadline even after its lane
    expired or a step it needs was dropped."""
    return bool(getattr(step, "kept", False))


def _kept_after(earlier: frozenset[str], step: Step, ctx: Context, found: pool.Pass, start: _Start) -> StepRecord:
    """Run the kept ``step`` once every handed off step of an earlier group in ``earlier`` ended, waiting at most its
    own budget for them, and run it even when one of them was dropped, unless the pass ended before it started."""
    found.ended(earlier, step.budget_s)
    if not start.begin():
        return StepRecord(step.name, 0.0, step.budget_s, abandoned=True, skipped=True)
    return run_step(step, ctx)


def _expire(
    running: dict[Future[StepRecord], tuple[Step, _Start, Deadline]],
    lost: tuple[set[str], set[str]],
    records: dict[str, StepRecord],
    found: pool.Pass,
    clock: Callable[[], float],
) -> None:
    """Stop waiting on every submitted step whose lane expired, recording it ended on the pass and in the dropped set
    of ``lost``, or in its cut set when only the reserve it leaves to a step that needs it ran out.

    A step that had not started is skipped and never starts. A step already running is recorded cut, its wall counted
    from its own start and its writes dropped; its run goes on under its own budget, and only a run past that budget
    journals it abandoned.
    """
    dropped, cut = lost
    for future, (step, start, lane) in list(running.items()):
        if lane.expired:
            running.pop(future)
            (dropped if _drops(step, dropped, found) else cut).add(step.name)
            began = start.close()
            if began is None:
                left = StepRecord(step.name, 0.0, step.budget_s, abandoned=True, skipped=True)
            else:
                _lost_writes(step, CUT_EVENT)
                left = StepRecord(step.name, clock() - began, step.budget_s, cut=True)
            records[step.name] = _ended(found, left)


def _eager(
    ordered: tuple[Step, ...],
    ctx: Context,
    found: pool.Pass,
    apply: Appliers,
    rest: frozenset[str] = frozenset(),
    gate: Gate | None = None,
    awaited: frozenset[str] = frozenset(),
) -> tuple[StepRecord, ...]:
    """Run ``ordered`` on the pass workers, each step submitted the moment its needs have ended, those in ``awaited``
    that another schedule of the pass runs included.

    Each finished step's writes are applied through ``apply`` on this scheduling thread before its dependents start.
    A step in ``rest`` starts no thread and ends at once for its dependents. A kept step starts even after its lane
    expired or a step it needs was dropped, bounded by its own budget and the pass deadline. A sweep that ends a step
    without a thread sweeps again at once, so a step whose needs that ended starts before the running steps end.
    """
    clock = ctx.deadline.clock
    names = {step.name for step in ordered}
    records: dict[str, StepRecord] = {}
    dropped: set[str] = set()
    cut: set[str] = set()
    pending = list(ordered)
    running: dict[Future[StepRecord], tuple[Step, _Start, Deadline]] = {}
    offsets: dict[str, float | None] = {}
    submitted: list[Future[StepRecord]] = []
    held_back = _held_back(ordered, found.reserves)
    try:
        while pending or running:
            ready = [
                step
                for step in pending
                if step.needs & names <= records.keys() and not _waiting(step, names, found, awaited)
            ]
            for step in ready:
                pending.remove(step)
                lane = _lane_of(step, found, held_back)
                offsets[step.name] = _offset()
                if step.name in rest | _shut(gate, step, records):
                    records[step.name] = _ended(found, _rested(step))
                elif not _kept(step) and (cut_by := _cut_off(step, lane, (dropped, cut), found, held_back)) is not None:
                    cut_by.add(step.name)
                    records[step.name] = _ended(
                        found, StepRecord(step.name, 0.0, step.budget_s, abandoned=True, skipped=True)
                    )
                elif _kept(step):
                    earlier = _earlier(step, names, found, awaited)
                    start = _Start(clock)
                    future = found.executor(step.name).submit(
                        copy_context().run, _kept_after, earlier, step, ctx, found, start
                    )
                    submitted.append(future)
                    running[future] = (step, start, ctx.deadline)
                else:
                    earlier = _earlier(step, names, found, awaited)
                    lane_ctx = _lane_context(lane, ctx)
                    start = _Start(clock)
                    future = found.executor(step.name).submit(
                        copy_context().run, _after, earlier, step, lane_ctx, found, start
                    )
                    submitted.append(future)
                    running[future] = (step, start, lane)
            ended = [0.0] if any(step.name in records for step in ready) else []
            held = any(_waiting(step, names, found, awaited) for step in pending)
            if not running:
                if not ready and not held:
                    raise ValueError(f"steps {sorted(step.name for step in pending)} wait on needs that never end")
                if held:
                    time.sleep(SETTLE_S)
                continue
            timeout = min([lane.remaining for _, _, lane in running.values()] + ([SETTLE_S] if held else []) + ended)
            done, _ = wait(running, timeout=timeout, return_when=FIRST_COMPLETED)
            for future in done:
                step, _, _ = running.pop(future)
                records[step.name] = _ended(found, _applied(future.result(), apply))
                dropped.update({step.name} if records[step.name].skipped else ())
            _expire(running, (dropped, cut), records, found, clock)
    finally:
        for future in submitted:
            future.cancel()
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
        "lease TTL": lease.ttl_ms() >= 2 * lease.tick_ms(),
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
