"""The handed lane starts with the pass graph, and a reserved step holds no later site behind it."""

from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Event
from time import sleep

import pytest

import scripts.swarm.cli
from scripts.tick import engine, pool, registry
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain, Lane, Result

pytestmark = pytest.mark.unit

CRITICAL_S = 1.0
SLOW_S = 0.9
OVERRUN_S = 1.5
BUSY_S = 1.2


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
    kept: bool = False

    def run(self, ctx: Context) -> Result:
        return self.action(ctx)


def _overrun(ctx: Context) -> Result:
    sleep(OVERRUN_S)
    return Result()


def _pass_graph() -> dict[str, frozenset[str]]:
    sites = scripts.swarm.cli.PASS_SITES
    order = {site: index for index, site in enumerate(sites)}
    found = sorted((s for s in registry.steps() if s.group in order), key=lambda s: order[s.group])
    edged = registry.site_edges(found, sites, scripts.swarm.cli.PASS_HOOK_KEYS, scripts.swarm.cli.PASS_SHUT_KEYS)
    return {step.name: step.needs for step in edged}


@pytest.mark.feature("swarm-tick-slow-lane-start")
def test_a_critical_overrun_leaves_every_handed_step_running_inside_the_slow_lane() -> None:
    later: list[tuple[engine.StepRecord, ...]] = []
    steps = (
        Step("overrun beside the lane", "a", _overrun),
        Step("fast", "a"),
        Step("slow free", "a", lane=engine.SLOW),
        Step("slow after fast", "b", needs=frozenset({"fast"}), lane=engine.SLOW),
        Step("after slow", "b", needs=frozenset({"slow free"}), lane=engine.SLOW),
    )
    ctx = Context(View(), Deadline.after(10.0))
    with pool.running(workers=4, critical_s=CRITICAL_S, slow_s=SLOW_S):
        engine.run_graph(steps, ctx, finished=later.append)
    assert len(later) == 1
    outcomes = {record.name: record.outcome for record in later[0]}
    assert outcomes == {"slow free": engine.OK, "slow after fast": engine.OK, "after slow": engine.OK}


@pytest.mark.feature("swarm-tick-slow-lane-start")
def test_a_handed_step_needing_a_lost_critical_step_is_still_dropped() -> None:
    later: list[tuple[engine.StepRecord, ...]] = []
    steps = (
        Step("overrun before a drop", "a", _overrun),
        Step("slow after overrun", "b", needs=frozenset({"overrun before a drop"}), lane=engine.SLOW),
        Step("through the dropped", "c", needs=frozenset({"slow after overrun"}), lane=engine.SLOW),
    )
    ctx = Context(View(), Deadline.after(10.0))
    with pool.running(workers=2, critical_s=CRITICAL_S, slow_s=CRITICAL_S + 0.5):
        engine.run_graph(steps, ctx, finished=later.append)
    assert [(record.name, record.skipped) for record in later[0]] == [
        ("slow after overrun", True),
        ("through the dropped", True),
    ]


def _busy(ctx: Context) -> Result:
    sleep(BUSY_S)
    return Result()


@pytest.mark.feature("swarm-tick-slow-lane-start")
def test_a_busy_handed_lane_leaves_the_critical_steps_their_workers() -> None:
    later: list[tuple[engine.StepRecord, ...]] = []
    busy = tuple(Step(f"busy {index}", "a", _busy, lane=engine.SLOW) for index in range(2))
    steps = (*busy, Step("critical beside busy", "a"))
    ctx = Context(View(), Deadline.after(10.0))
    with pool.running(workers=2, critical_s=CRITICAL_S, slow_s=2 * BUSY_S + 1.0):
        records = engine.run_graph(steps, ctx, finished=later.append)
    assert [(record.name, record.outcome) for record in records] == [("critical beside busy", engine.OK)]
    assert {record.outcome for record in later[0]} == {engine.OK}


@pytest.mark.feature("swarm-tick-slow-lane-start")
def test_a_handed_step_waiting_on_a_critical_need_holds_no_handed_worker() -> None:
    later: list[tuple[engine.StepRecord, ...]] = []
    free_ran = Event()

    def critical(ctx: Context) -> Result:
        assert free_ran.wait(BUSY_S), "the free handed step never got the handed worker"
        return Result()

    def free(ctx: Context) -> Result:
        free_ran.set()
        return Result()

    steps = (
        Step("critical awaited", "a", critical),
        Step("waits on critical", "a", needs=frozenset({"critical awaited"}), lane=engine.SLOW),
        Step("free beside the wait", "a", free, lane=engine.SLOW),
    )
    ctx = Context(View(), Deadline.after(10.0))
    with pool.running(workers=1, critical_s=2 * BUSY_S, slow_s=2 * BUSY_S):
        records = engine.run_graph(steps, ctx, finished=later.append)
    assert [(record.name, record.outcome) for record in records] == [("critical awaited", engine.OK)]
    assert {record.name: record.outcome for record in later[0]} == {
        "waits on critical": engine.OK,
        "free beside the wait": engine.OK,
    }


@pytest.mark.feature("swarm-tick-slow-lane-start")
@pytest.mark.parametrize("is_kept", [True, False], ids=["kept", "plain"])
def test_a_kept_handed_step_runs_on_the_handed_workers(is_kept: bool) -> None:
    later: list[tuple[engine.StepRecord, ...]] = []
    critical_started, kept_ran = Event(), Event()

    def critical(ctx: Context) -> Result:
        critical_started.set()
        assert kept_ran.wait(BUSY_S), "the kept handed step shared the critical step's only worker"
        return Result()

    def kept(ctx: Context) -> Result:
        assert critical_started.wait(BUSY_S), "the critical step shared the kept handed step's only worker"
        kept_ran.set()
        return Result()

    steps = (Step("critical holding", "a", critical), Step("kept handed", "a", kept, lane=engine.SLOW, kept=is_kept))
    ctx = Context(View(), Deadline.after(10.0))
    with pool.running(workers=1, critical_s=2 * BUSY_S, slow_s=2 * BUSY_S):
        records = engine.run_graph(steps, ctx, finished=later.append)
    assert [(record.name, record.outcome) for record in records] == [("critical holding", engine.OK)]
    assert [record.outcome for record in later[0]] == [engine.OK]
