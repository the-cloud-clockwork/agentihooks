"""The handed lane's own workers, the pass gate across the handed lane and the rest of the graph, and its needs."""

from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Barrier, BrokenBarrierError, Event, current_thread
from time import monotonic, sleep

import pytest

from scripts.tick import engine, pool
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain, Lane, Result
from scripts.tick.step import Step as Stepping

pytestmark = pytest.mark.unit

HOOK_S = 0.4


class View:
    def read(self, domain: Domain, key: str) -> object | None:
        return None


@dataclass
class Step:
    name: str
    group: str
    action: Callable[[Context], Result] = field(default=lambda ctx: Result())
    needs: frozenset[str] = frozenset()
    lane: Lane = Lane("critical")
    budget_s: float = 5.0
    interval_s: float = 0.0
    reads: frozenset[Domain] = frozenset()
    writes: frozenset[Domain] = frozenset()
    effects: frozenset[str] = frozenset()
    feature: str = ""

    def run(self, ctx: Context) -> Result:
        return self.action(ctx)


def _ctx() -> Context:
    return Context(View(), Deadline.after(10.0))


def _never_shut(step: Stepping) -> bool:
    return False


def _slow_hook() -> bool:
    sleep(HOOK_S)
    return True


def _pause(ctx: Context) -> Result:
    sleep(0.05)
    return Result()


def _thread_name() -> str:
    return current_thread().name


def _meet(barrier: Barrier) -> bool:
    try:
        barrier.wait()
    except BrokenBarrierError:
        return False
    return True


@pytest.mark.feature("swarm-tick-slow-lane-start")
def test_the_handed_workers_are_named_and_bounded_by_the_pass_workers() -> None:
    barrier = Barrier(2, timeout=0.5)
    with pool.running(workers=1) as found:
        assert found.handoff is not None
        assert found.handoff.submit(_thread_name).result().startswith("tick-handed_")
        met = [found.handoff.submit(_meet, barrier) for _ in range(2)]
        assert [future.result() for future in met] == [False, False]


@pytest.mark.wall_clock
@pytest.mark.feature("swarm-tick-slow-lane-start")
def test_a_pass_ends_without_waiting_on_its_handed_workers_and_cancels_their_queue() -> None:
    gate, started = Event(), Event()

    def blocker() -> None:
        started.set()
        gate.wait(5)

    try:
        began = monotonic()
        with pool.running(workers=1) as found:
            assert found.handoff is not None
            found.handoff.submit(blocker)
            assert started.wait(5)
            queued = found.handoff.submit(_thread_name)
        assert monotonic() - began < 2.0
        assert queued.cancelled()
        with pytest.raises(RuntimeError):
            found.handoff.submit(_thread_name)
    finally:
        gate.set()


@pytest.mark.feature("swarm-tick-slow-lane-start")
def test_a_handed_step_waits_for_a_site_hook_running_on_the_critical_thread_past_the_critical_lane() -> None:
    later: list[tuple[engine.StepRecord, ...]] = []
    steps = (
        Step("site critical", "a"),
        Step("handed pre", "z", _pause, lane=engine.SLOW),
        Step("site handed", "a", needs=frozenset({"handed pre"}), lane=engine.SLOW),
    )
    gate = engine.SiteGate({"a": _slow_hook}, _never_shut)
    with pool.running(workers=2, critical_s=HOOK_S / 4, slow_s=6 * HOOK_S):
        engine.run_graph(steps, _ctx(), finished=later.append, gate=gate)
    assert [(record.name, record.outcome) for record in later[0]] == [
        ("handed pre", engine.OK),
        ("site handed", engine.OK),
    ]


@pytest.mark.feature("swarm-tick-slow-lane-start")
def test_a_step_waits_for_its_site_hook_no_longer_than_its_own_budget() -> None:
    later: list[tuple[engine.StepRecord, ...]] = []
    steps = (
        Step("short critical", "a", budget_s=HOOK_S / 4),
        Step("short handed", "a", lane=engine.SLOW, budget_s=HOOK_S / 4),
    )
    gate = engine.SiteGate({"a": _slow_hook}, _never_shut)
    with pool.running(workers=2, critical_s=6 * HOOK_S, slow_s=6 * HOOK_S):
        records = engine.run_graph(steps, _ctx(), finished=later.append, gate=gate)
    rested = sorted(record.rested for record in (*records, *later[0]))
    assert rested == [False, True]


@pytest.mark.feature("swarm-tick-slow-lane-start")
def test_a_site_whose_hook_says_no_rests_its_steps() -> None:
    gate = engine.SiteGate({"a": lambda: False}, _never_shut)
    with pool.running(workers=2):
        records = engine.run_graph((Step("refused", "a"),), _ctx(), gate=gate)
    assert [(record.name, record.rested) for record in records] == [("refused", True)]


@pytest.mark.feature("swarm-tick-slow-lane-start")
def test_the_gate_rests_a_handed_step_it_shuts() -> None:
    later: list[tuple[engine.StepRecord, ...]] = []

    def shut(step: Stepping) -> bool:
        return step.name == "handed shut"

    gate = engine.SiteGate({}, shut)
    with pool.running(workers=2):
        engine.run_graph((Step("handed shut", "a", lane=engine.SLOW),), _ctx(), finished=later.append, gate=gate)
    assert [(record.name, record.rested) for record in later[0]] == [("handed shut", True)]


@pytest.mark.feature("swarm-tick-slow-lane-start")
def test_a_handed_step_needing_a_step_an_earlier_group_ran_starts_at_once() -> None:
    later: list[tuple[engine.StepRecord, ...]] = []
    with pool.running(workers=2, critical_s=1.0, slow_s=1.0):
        engine.run_group("earlier", _ctx(), (Step("ran before", "earlier"),))
        needing = Step("needs the earlier", "after", needs=frozenset({"ran before"}), lane=engine.SLOW)
        engine.run_group("after", _ctx(), (needing,), finished=later.append)
    assert [(record.name, record.outcome) for record in later[0]] == [("needs the earlier", engine.OK)]
