from dataclasses import dataclass, field
from threading import Event

import pytest

from scripts.tick import engine
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain, Lane, Result

pytestmark = pytest.mark.unit


class View:
    def read(self, domain: Domain, key: str) -> object | None:
        return None


@dataclass
class BlockingStep:
    name: str
    budget_s: float = 0.1
    order: int = 10
    group: str = "placement"
    lane: Lane = Lane("tick")
    interval_s: float = 60.0
    needs: frozenset[str] = frozenset()
    writes: frozenset[Domain] = frozenset()
    release: Event = field(default_factory=Event)
    ended: Event = field(default_factory=Event)
    runs: list[int] = field(default_factory=list)

    def run(self, ctx: Context) -> Result:
        self.runs.append(len(self.runs) + 1)
        try:
            self.release.wait(300.0)
        finally:
            self.ended.set()
        return Result()


def _ctx() -> Context:
    return Context(View(), Deadline.after(600.0))


def _finish(step: BlockingStep) -> None:
    step.release.set()
    assert step.ended.wait(30.0)


@pytest.mark.feature("swarm-tick-step-overlap-hold")
def test_a_step_whose_earlier_run_is_alive_is_held_without_running() -> None:
    step = BlockingStep("overlap held step")
    try:
        first = engine.run_step(step, _ctx())
        second = engine.run_step(step, _ctx())
    finally:
        _finish(step)
    assert first.outcome == engine.LATE
    assert second == engine.StepRecord("overlap held step", 0.0, 0.1, held=True)
    assert second.outcome == engine.HELD
    assert step.runs == [1]


@pytest.mark.feature("swarm-tick-step-overlap-hold")
def test_a_step_runs_again_once_its_earlier_run_ended() -> None:
    step = BlockingStep("overlap rerun step")
    try:
        engine.run_step(step, _ctx())
    finally:
        _finish(step)
    step.ended.clear()
    step.release.set()
    record = engine.run_step(step, _ctx())
    assert (record.outcome, record.held, record.abandoned) == (engine.OK, False, False)
    assert step.runs == [1, 2]


@pytest.mark.feature("swarm-tick-step-overlap-hold")
def test_another_step_is_not_held_by_a_running_one() -> None:
    stuck = BlockingStep("overlap stuck step")
    other = BlockingStep("overlap other step", budget_s=5.0)
    other.release.set()
    try:
        engine.run_step(stuck, _ctx())
        record = engine.run_step(other, _ctx())
    finally:
        _finish(stuck)
    assert (record.outcome, record.held) == (engine.OK, False)
    assert other.runs == [1]


@pytest.mark.feature("swarm-tick-step-overlap-hold")
def test_placement_runs_once_across_two_overlapping_passes() -> None:
    spawn = BlockingStep("spawn")
    try:
        first = engine.run_group("placement", _ctx(), (spawn,))
        second = engine.run_group("placement", _ctx(), (spawn,))
    finally:
        _finish(spawn)
    assert [(record.name, record.outcome) for record in first] == [("spawn", engine.LATE)]
    assert [(record.name, record.outcome) for record in second] == [("spawn", engine.HELD)]
    assert spawn.runs == [1]


@pytest.mark.feature("swarm-tick-step-overlap-hold")
def test_a_held_record_reads_held_unless_it_failed() -> None:
    assert engine.StepRecord("x", 0.0, 5.0, held=True).outcome == engine.HELD
    assert engine.StepRecord("x", 0.0, 5.0, error="boom", held=True).outcome == engine.FAILED
    assert engine.HELD == "held"
