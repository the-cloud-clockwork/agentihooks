from dataclasses import dataclass, field
from threading import Event, Thread, current_thread, main_thread
from time import monotonic

import pytest

import scripts.swarm.cli  # noqa: F401
from scripts.tick import engine, registry
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain, Lane, Result, Step
from scripts.tick.steps import CallStep, agents, budgets, host, masters, observe, place, report, tasks

pytestmark = pytest.mark.unit


class View:
    def read(self, domain: Domain, key: str) -> object | None:
        return None


@dataclass
class TimedStep:
    name: str
    order: int
    budget_s: float
    needs: frozenset[str] = frozenset()
    hold: Event | None = None
    group: str = "place"
    lane: Lane = Lane("tick")
    interval_s: float = 60.0
    writes: frozenset[Domain] = frozenset()
    started: list[float] = field(default_factory=list)

    def run(self, ctx: Context) -> Result:
        self.started.append(monotonic())
        if self.hold is not None:
            self.hold.wait(300.0)
        return Result()


@pytest.mark.feature("swarm-tick-step-isolation")
def test_a_300_second_step_before_spawn_no_longer_delays_spawn() -> None:
    hold = Event()
    stuck = TimedStep("stuck", 10, 0.2, hold=hold)
    spawn = TimedStep("spawn", 20, 1.0)
    began = monotonic()
    try:
        records = engine.run_group("place", Context(View(), Deadline.after(600.0)), (stuck, spawn))
    finally:
        hold.set()
    assert spawn.started and spawn.started[0] - began < 5.0
    assert [(record.name, record.outcome) for record in records] == [("stuck", engine.LATE), ("spawn", engine.OK)]
    assert records[0].abandoned is True
    assert records[0].result is None
    assert records[1].abandoned is False


@pytest.mark.feature("swarm-tick-step-isolation")
def test_a_step_that_ends_within_its_budget_is_kept_with_its_result() -> None:
    record = engine.run_step(TimedStep("quick", 1, 5.0), Context(View(), Deadline.after(60.0)))
    assert (record.outcome, record.abandoned, record.result, record.error) == (engine.OK, False, Result(), "")


@pytest.mark.feature("swarm-tick-step-isolation")
def test_an_abandoned_step_keeps_its_wall_time_and_a_daemon_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    readings = iter([0.0, 0.0, 10.0, 10.25])
    made: list[Thread] = []

    def clock() -> float:
        return next(readings) if current_thread() is main_thread() else 0.0

    class Recording(Thread):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # pyright: ignore[reportArgumentType]
            made.append(self)

    monkeypatch.setattr(engine, "Thread", Recording)
    hold = Event()
    try:
        record = engine.run_step(
            TimedStep("stuck", 1, 0.1, hold=hold), Context(View(), Deadline.after(600.0, clock=clock))
        )
    finally:
        hold.set()
    assert record == engine.StepRecord("stuck", 0.25, 0.1, abandoned=True)
    assert [thread.daemon for thread in made] == [True]


@pytest.mark.feature("swarm-tick-step-isolation")
def test_an_abandoned_record_reads_late_even_inside_its_budget() -> None:
    assert engine.StepRecord("left", 0.1, 5.0, abandoned=True).outcome == engine.LATE
    assert engine.StepRecord("left", 0.1, 5.0, error="x", abandoned=True).outcome == engine.FAILED


@pytest.mark.feature("swarm-tick-step-isolation")
def test_budgets_come_from_the_p95_with_headroom_in_whole_seconds() -> None:
    assert budgets.budget_s(0.0) == 1.0
    assert budgets.budget_s(0.6) == 1.0
    assert budgets.budget_s(0.7) == 2.0
    assert budgets.budget_s(14.511) == 22.0
    assert budgets.timing_of("preempt") == (18.0, 60.0)
    assert budgets.timing_of("night watch") == (22.0, 120.0)
    assert budgets.timing_of("config load") == (15.0, 60.0)
    assert budgets.timing_of("hive ledger") == (15.0, 120.0)


@pytest.mark.feature("swarm-tick-step-isolation")
def test_an_undeclared_step_is_refused() -> None:
    with pytest.raises(ValueError) as raised:
        CallStep("never measured", "place", 1, lambda tick, get: None)
    assert str(raised.value) == "Undeclared step timing: never measured"


@pytest.mark.feature("swarm-tick-step-isolation")
def test_every_registered_call_step_declares_its_measured_timing() -> None:
    for step in (*observe.STEPS, *agents.STEPS, *masters.STEPS, *tasks.STEPS, *place.STEPS, *host.STEPS, *report.STEPS):
        assert (step.budget_s, step.interval_s) == budgets.timing_of(step.name)
        assert step.interval_s in {budgets.CRITICAL_S, budgets.WIDE_S, budgets.SLOW_S}


@pytest.mark.feature("swarm-tick-step-isolation")
def test_spawn_needs_only_judge_orphans_and_preempt() -> None:
    assert budgets.JUDGE == {"verify", "reap", "launch checks", "track outages", "overrun pass", "quiet pass"}
    assert budgets.SPAWN_NEEDS == budgets.JUDGE | {"orphans", "preempt"}
    assert {step.name: step.needs for step in place.STEPS}["spawn"] == budgets.SPAWN_NEEDS
    registered = [step for wave in registry.waves() for step in wave]
    assert registry.latest_start("spawn", registered) <= budgets.SPAWN_START_S


@pytest.mark.feature("swarm-tick-step-isolation")
def test_spawn_starts_by_forty_seconds_into_the_pass() -> None:
    needed = [TimedStep(name, 10, budgets.timing_of(name)[0]) for name in sorted(budgets.SPAWN_NEEDS)]
    slow = TimedStep("night watch", 5, 300.0)
    spawn = TimedStep("spawn", 20, budgets.timing_of("spawn")[0], needs=budgets.SPAWN_NEEDS)
    start = registry.latest_start("spawn", [slow, *needed, spawn])
    assert start == 22.0
    assert start <= budgets.SPAWN_START_S


@pytest.mark.feature("swarm-tick-step-isolation")
def test_latest_start_follows_the_longest_needs_path() -> None:
    first = TimedStep("a", 1, 3.0)
    second = TimedStep("b", 2, 4.0, needs=frozenset({"a"}))
    side = TimedStep("c", 3, 5.0)
    last = TimedStep("d", 4, 1.0, needs=frozenset({"b", "c"}))
    steps: list[Step] = [first, second, side, last]
    assert [registry.latest_start(name, steps) for name in "abcd"] == [0.0, 3.0, 0.0, 7.0]
