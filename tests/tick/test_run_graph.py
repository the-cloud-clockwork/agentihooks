"""The flat pass graph: barrier edges from the group site table and one schedule for every step of a pass."""

from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Event, Lock
from time import monotonic

import pytest

from scripts.tick import engine, pool, registry
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain, Lane, Result, Write

pytestmark = pytest.mark.unit

PASS = Domain("pass")
SHARED = frozenset({Domain("pass:shared")})


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


def _needs(steps: tuple[object, ...]) -> dict[str, frozenset[str]]:
    return {step.name: step.needs for step in steps}


def test_site_edges_make_each_step_need_every_step_of_the_previous_site() -> None:
    steps = (
        Step("a1", "a", writes=SHARED),
        Step("a2", "a", reads=SHARED),
        Step("b1", "b", needs=frozenset({"a1"}), writes=SHARED),
        Step("c1", "c", reads=SHARED),
    )
    edged = registry.site_edges(steps, ("a", "b", "c"))
    assert _needs(edged) == {
        "a1": frozenset(),
        "a2": frozenset(),
        "b1": frozenset({"a1", "a2"}),
        "c1": frozenset({"b1"}),
    }


def test_site_edges_skip_an_empty_site_and_leave_steps_outside_the_table_alone() -> None:
    steps = (Step("a1", "a", writes=SHARED), Step("b1", "b", reads=SHARED), Step("x1", "x", needs=frozenset({"a1"})))
    edged = registry.site_edges(steps, ("a", "empty", "b"))
    assert _needs(edged) == {"a1": frozenset(), "b1": frozenset({"a1"}), "x1": frozenset({"a1"})}


def test_site_edges_keep_order_name_group_lane_and_run_of_each_step() -> None:
    ran: list[str] = []
    first = Step("a1", "a", lambda ctx: ran.append("a1") or Result(), lane=Lane("slow"), budget_s=2.0)
    edged = registry.site_edges((first, Step("b1", "b")), ("a", "b"))
    assert [step.name for step in edged] == ["a1", "b1"]
    assert (edged[0].group, edged[0].lane, edged[0].budget_s) == ("a", Lane("slow"), 2.0)
    edged[0].run(_ctx())
    assert ran == ["a1"]


def test_run_graph_runs_every_step_in_needs_order_outside_a_parallel_pass_and_applies_writes() -> None:
    ran: list[str] = []
    applied: list[object] = []

    def action(name: str) -> Callable[[Context], Result]:
        return lambda ctx: ran.append(name) or Result(writes=(Write(PASS, name, name),))

    steps = (
        Step("late", "b", action("late"), needs=frozenset({"early"})),
        Step("early", "a", action("early")),
        Step("free", "x", action("free")),
    )
    records = engine.run_graph(steps, _ctx(), {PASS: lambda write: applied.append(write.value) or None})
    assert [record.name for record in records] == ["early", "late", "free"]
    assert ran == ["early", "late", "free"]
    assert applied == ["early", "late", "free"]
    assert all(record.outcome == engine.OK for record in records)


def test_run_graph_overlaps_a_step_with_no_edge_and_holds_a_barrier_step_until_its_site_ends() -> None:
    times: dict[str, tuple[float, float]] = {}
    lock = Lock()
    release = Event()

    def timed(name: str, gate: Event | None = None) -> Callable[[Context], Result]:
        def action(ctx: Context) -> Result:
            start = monotonic()
            if gate is not None:
                gate.wait(5.0)
            with lock:
                times[name] = (start, monotonic())
            return Result()

        return action

    def freeing(ctx: Context) -> Result:
        start = monotonic()
        release.set()
        with lock:
            times["intent"] = (start, monotonic())
        return Result()

    steps = registry.site_edges(
        (
            Step("probe", "probes", timed("probe", release), writes=SHARED),
            Step("observe", "observe", timed("observe"), reads=SHARED),
            Step("intent", "judge", freeing),
        ),
        ("probes", "observe"),
    )
    with pool.running(workers=3):
        records = engine.run_graph(steps, _ctx())
    assert [record.outcome for record in records] == [engine.OK] * 3
    assert times["intent"][0] < times["probe"][1]
    assert times["observe"][0] >= times["probe"][1]


def test_run_graph_hands_slow_lane_steps_and_their_dependents_to_finished() -> None:
    later: list[tuple[engine.StepRecord, ...]] = []
    steps = (
        Step("fast", "a"),
        Step("slow", "a", lane=engine.SLOW),
        Step("after slow", "b", needs=frozenset({"slow"})),
    )
    with pool.running(workers=2):
        records = engine.run_graph(steps, _ctx(), finished=later.append)
        assert [record.name for record in records] == ["fast"]
    assert [[record.name for record in group] for group in later] == [["slow", "after slow"]]
