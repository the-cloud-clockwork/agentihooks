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
def test_site_barriers_of_the_pass_graph_hold_no_critical_step_on_a_slow_step() -> None:
    sites = scripts.swarm.cli.PASS_SITES
    order = {site: index for index, site in enumerate(sites)}
    found = sorted((s for s in registry.steps() if s.group in order), key=lambda s: order[s.group])
    edged = registry.site_edges(found, sites, scripts.swarm.cli.PASS_HOOK_KEYS, scripts.swarm.cli.PASS_SHUT_KEYS)
    by_name = {step.name: step for step in edged}
    slow = {name for name, step in by_name.items() if step.lane == engine.SLOW}
    broken = sorted(
        f"{name} needs {sorted(_upstream(name, by_name) & slow)}"
        for name in CRITICAL_STEPS & by_name.keys()
        if _upstream(name, by_name) & slow
    )
    assert broken == [], f"{len(broken)} critical steps wait on the slow lane through site barriers: {broken}"


@pytest.mark.feature("swarm-tick-step-isolation")
def test_pass_lanes_are_sized_for_the_minute() -> None:
    assert pool.CRITICAL_S == 60.0
    assert pool.SLOW_S == 59.0
    assert engine.cadence_breaches(pool.SLOW_S) == []


@pytest.mark.feature("swarm-tick-step-isolation")
def test_the_relay_and_view_sites_wait_for_the_intent_check_their_hooks_read() -> None:
    sites = scripts.swarm.cli.PASS_SITES
    order = {site: index for index, site in enumerate(sites)}
    found = sorted((s for s in registry.steps() if s.group in order), key=lambda s: order[s.group])
    edged = registry.site_edges(found, sites, scripts.swarm.cli.PASS_HOOK_KEYS, scripts.swarm.cli.PASS_SHUT_KEYS)
    by_name = {step.name: step for step in edged}
    assert "intent check" in by_name["outbox migration"].needs
    assert "intent check" in by_name["page relay"].needs
    assert "page relay" in by_name["tick view"].needs


@pytest.mark.feature("swarm-tick-step-isolation")
def test_the_quiet_pass_waits_for_the_steps_of_the_sites_before_it() -> None:
    sites = scripts.swarm.cli.PASS_SITES
    order = {site: index for index, site in enumerate(sites)}
    found = sorted((s for s in registry.steps() if s.group in order), key=lambda s: order[s.group])
    edged = registry.site_edges(found, sites, scripts.swarm.cli.PASS_HOOK_KEYS, scripts.swarm.cli.PASS_SHUT_KEYS)
    by_name = {step.name: step for step in edged}
    quiet = by_name["quiet pass"]
    assert quiet.needs
    assert all(order[by_name[name].group] < order[quiet.group] for name in quiet.needs)
