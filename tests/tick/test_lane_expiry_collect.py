"""A step still running when its lane expires is recorded cut; one that already finished keeps its real result."""

from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Event
from time import sleep

import pytest

from scripts.tick import engine, pool
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain, Lane, Result, Write

pytestmark = pytest.mark.unit

REAPED = Domain("reaped")
GONE = Write(REAPED, "agent", "gone")


class View:
    def read(self, domain: Domain, key: str) -> object | None:
        return None


@dataclass
class Step:
    name: str
    group: str
    action: Callable[[Context], Result] = field(default=lambda ctx: Result())
    needs: frozenset[str] = frozenset()
    lane: Lane = Lane("tick")
    budget_s: float = 5.0
    interval_s: float = 0.0
    reads: frozenset[Domain] = frozenset()
    writes: frozenset[Domain] = frozenset()
    effects: frozenset[str] = frozenset()
    feature: str = ""

    def run(self, ctx: Context) -> Result:
        return self.action(ctx)


def _reaping(release: Event) -> Callable[[Context], Result]:
    def action(ctx: Context) -> Result:
        release.wait(3.0)
        return Result((GONE,))

    return action


def _recording(applied: list[Write]) -> Callable[[Write], list[str]]:
    def applier(write: Write) -> list[str]:
        applied.append(write)
        return [f"reaped {write.key}"]

    return applier


def _records(
    first: tuple[engine.StepRecord, ...], later: list[tuple[engine.StepRecord, ...]]
) -> dict[str, engine.StepRecord]:
    return {record.name: record for record in (*first, *(record for batch in later for record in batch))}


@pytest.mark.feature("tick-finished-step-ends-at-lane-expiry")
def test_a_step_still_running_at_its_lane_expiry_is_recorded_cut_with_its_writes_and_dependent_dropped() -> None:
    release = Event()
    applied: list[Write] = []
    steps = (
        Step("reap", "lane end", _reaping(release), writes=frozenset({REAPED})),
        Step("orphans", "lane end", needs=frozenset({"reap"}), lane=Lane("slow")),
    )
    later: list[tuple[engine.StepRecord, ...]] = []
    try:
        with pool.running(critical_s=0.3, slow_s=5.0, reserves={}):
            first = engine.run_graph(
                steps, Context(View(), Deadline.after(10.0)), {REAPED: _recording(applied)}, later.append
            )
    finally:
        release.set()
    records = _records(first, later)
    assert records["reap"].cut
    assert records["reap"].outcome == engine.CUT
    assert applied == []
    assert records["orphans"].skipped


@pytest.mark.feature("tick-finished-step-ends-at-lane-expiry")
def test_a_step_that_finished_before_its_lane_expired_but_was_collected_after_keeps_its_result() -> None:
    release = Event()
    applied: list[Write] = []

    def busy(write: Write) -> list[str]:
        release.set()
        sleep(0.6)
        return []

    steps = (
        Step("busy", "lane end", lambda ctx: Result((Write(Domain("busy"), "pass", 1),))),
        Step("reap", "lane end", _reaping(release), writes=frozenset({REAPED})),
        Step("orphans", "lane end", needs=frozenset({"reap"}), lane=Lane("slow")),
    )
    later: list[tuple[engine.StepRecord, ...]] = []
    apply = {Domain("busy"): busy, REAPED: _recording(applied)}
    try:
        with pool.running(critical_s=0.3, slow_s=5.0, reserves={}):
            first = engine.run_graph(steps, Context(View(), Deadline.after(10.0)), apply, later.append)
    finally:
        release.set()
    records = _records(first, later)
    assert records["reap"].outcome == engine.OK
    assert records["reap"].applied == ("reaped agent",)
    assert applied == [GONE]
    assert records["orphans"].outcome == engine.OK
