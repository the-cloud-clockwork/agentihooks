from dataclasses import dataclass, field
from threading import Event, Thread, current_thread, main_thread
from time import monotonic

import pytest

import scripts.swarm.cli  # noqa: F401
from scripts.tick import engine, pool, registry
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
    budget_s: float
    needs: frozenset[str] = frozenset()
    hold: Event | None = None
    group: str = "place"
    lane: Lane = Lane("tick")
    interval_s: float = 60.0
    reads: frozenset[Domain] = frozenset()
    writes: frozenset[Domain] = frozenset()
    effects: frozenset[str] = frozenset()
    feature: str = ""
    started: list[float] = field(default_factory=list)

    def run(self, ctx: Context) -> Result:
        self.started.append(monotonic())
        if self.hold is not None:
            self.hold.wait(300.0)
        return Result()


@pytest.mark.feature("swarm-tick-step-isolation")
def test_a_300_second_step_before_spawn_no_longer_delays_spawn() -> None:
    hold = Event()
    stuck = TimedStep("stuck", 0.2, hold=hold)
    spawn = TimedStep("spawn", 1.0)
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
    record = engine.run_step(TimedStep("quick", 5.0), Context(View(), Deadline.after(60.0)))
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
            TimedStep("stuck", 0.1, hold=hold), Context(View(), Deadline.after(600.0, clock=clock))
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
    assert budgets.timing_of("config load") == (2.0, 60.0)
    assert budgets.timing_of("hive ledger") == (2.0, 120.0)


@pytest.mark.feature("swarm-tick-step-isolation")
def test_an_undeclared_step_is_refused() -> None:
    with pytest.raises(ValueError) as raised:
        CallStep("never measured", "place", lambda tick, get: None)
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


@pytest.mark.feature("swarm-tick-step-isolation")
def test_spawn_starts_by_forty_seconds_into_the_pass() -> None:
    needed = [TimedStep(name, budgets.timing_of(name)[0]) for name in sorted(budgets.SPAWN_NEEDS)]
    slow = TimedStep("night watch", 300.0)
    spawn = TimedStep("spawn", budgets.timing_of("spawn")[0], needs=budgets.SPAWN_NEEDS)
    start = registry.latest_start("spawn", [slow, *needed, spawn])
    assert start == 22.0
    assert start <= budgets.SPAWN_START_S


@pytest.mark.feature("swarm-tick-step-isolation")
def test_latest_start_follows_the_longest_needs_path() -> None:
    first = TimedStep("a", 3.0)
    second = TimedStep("b", 4.0, needs=frozenset({"a"}))
    side = TimedStep("c", 5.0)
    last = TimedStep("d", 1.0, needs=frozenset({"b", "c"}))
    steps: list[Step] = [first, second, side, last]
    assert [registry.latest_start(name, steps) for name in "abcd"] == [0.0, 3.0, 0.0, 7.0]


SLOW_STEPS = frozenset(
    {
        "lost runner",
        "time left",
        "release alert",
        "promotion refresh",
        "fleet red notice",
        "night watch",
        "master alarm",
        "master stall",
        "prompt stall",
        "retire idle master",
        "ledger watchdog",
        "ledger probe",
        "prompt backlog",
        "host pressure",
        "hive ledger",
        "pane evidence",
        "strays",
        "failed panes",
        "conversations",
        "session models",
        "doc report",
        "priorities sweep",
        "health findings",
        "findings pass",
        "metrics record",
        "bottleneck status",
        "inbox wake",
    }
)
CRITICAL_STEPS = frozenset(
    {
        "config load",
        "ledger state",
        "exits sweep",
        "launch checks",
        "track outages",
        "verify",
        "reap",
        "orphans",
        "recover outages",
        "sleeping",
        "drained resume",
        "run window",
        "size pass",
        "grouping release",
        "dispatcher group",
        "rank pass",
        "bottleneck remedy",
        "preempt",
        "spawn",
        "collect acks",
        "master down",
        "tick master",
        "master scale",
        "seat alarm",
        "dispatch seat",
        "settle",
        "close space",
        "stall watch",
        "overrun pass",
        "quiet pass",
    }
)


def _upstream(name: str, by_name: dict[str, Step]) -> set[str]:
    found: set[str] = set()
    pending = list(by_name[name].needs)
    while pending:
        need = pending.pop()
        if need not in found:
            found.add(need)
            pending.extend(by_name[need].needs)
    return found


@pytest.mark.feature("swarm-tick-step-isolation")
def test_notice_and_watch_steps_declare_the_slow_lane() -> None:
    lanes = {step.name: step.lane for step in registry.steps()}
    off = sorted(name for name in SLOW_STEPS if lanes[name] != engine.SLOW)
    assert off == [], f"{len(off)} of {len(SLOW_STEPS)} notice and watch steps off the slow lane: {off}"


@pytest.mark.feature("swarm-tick-step-isolation")
def test_critical_path_steps_stay_off_the_slow_lane_and_need_no_slow_step() -> None:
    by_name = {step.name: step for step in registry.steps()}
    slow = {name for name, step in by_name.items() if step.lane == engine.SLOW}
    broken = sorted(
        f"{name} needs {sorted(_upstream(name, by_name) & slow)}"
        for name in CRITICAL_STEPS
        if by_name[name].lane == engine.SLOW or _upstream(name, by_name) & slow
    )
    assert broken == [], f"{len(broken)} of {len(CRITICAL_STEPS)} critical steps wait on the slow lane: {broken}"


@pytest.mark.feature("swarm-tick-step-isolation")
def test_pass_lanes_are_sized_for_the_minute() -> None:
    assert pool.CRITICAL_S == 60.0
    assert pool.SLOW_S == 59.0
    assert engine.cadence_breaches(pool.SLOW_S) == []


@pytest.mark.feature("swarm-tick-step-isolation")
def test_site_edges_keep_only_barriers_whose_reads_and_writes_cross() -> None:
    rows, doc = Domain("pass:rows"), Domain("pass:doc")
    writer = TimedStep("writer", 1.0, group="a", writes=frozenset({rows}))
    reader = TimedStep("reader", 1.0, group="a", reads=frozenset({doc}))
    after = TimedStep("after", 1.0, group="b", reads=frozenset({rows}), writes=frozenset({doc}))
    free = TimedStep("free", 1.0, group="b")
    edged = registry.site_edges((writer, reader, after, free), ("a", "b"))
    assert {step.name: step.needs for step in edged} == {
        "writer": frozenset(),
        "reader": frozenset(),
        "after": frozenset({"writer", "reader"}),
        "free": frozenset(),
    }
