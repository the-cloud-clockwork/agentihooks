from threading import Event

import pytest

from scripts.tick import engine, pool, registry
from scripts.tick import steps as step_module
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain, Lane, Result
from tests.tick.test_pool import Step, context


@pytest.mark.feature("tick-site-ordering-does-not-propagate-loss")
def test_abandoned_page_relay_does_not_drop_barrier_only_slow_work() -> None:
    release = Event()
    ran: list[str] = []
    values = frozenset({Domain("pass.agents")})

    def relay(ctx: Context) -> Result:
        release.wait(30.0)
        return Result()

    def slow(ctx: Context) -> Result:
        ran.append("findings pass")
        return Result()

    steps = registry.site_edges(
        (
            Step("page relay", relay, group="relay", budget_s=0.02, writes=values),
            Step("findings pass", slow, lane=Lane("slow"), group="report", reads=values),
            Step("inbox wake", slow, frozenset({"page relay"}), lane=Lane("slow"), group="report"),
            Step("after wake", slow, frozenset({"inbox wake"}), lane=Lane("slow"), group="report"),
        ),
        ("relay", "report"),
    )
    assert steps[1].needs == frozenset({"page relay"})
    later: list[tuple[engine.StepRecord, ...]] = []
    try:
        with pool.running(workers=3, critical_s=2.0, slow_s=2.0):
            records = engine.run_graph(steps, context(), finished=later.append)
        assert [(row.name, row.abandoned) for row in records] == [("page relay", True)]
        assert ran == ["findings pass"]
        assert [[(row.name, row.outcome) for row in group] for group in later] == [
            [("findings pass", engine.OK), ("inbox wake", engine.LATE), ("after wake", engine.LATE)]
        ]
    finally:
        release.set()
        worker = engine._RUNNING.get((None, "page relay"))
        if worker is not None:
            worker.join(30.0)


def test_site_edges_keep_declared_needs_and_forward_the_step_contract() -> None:
    values = frozenset({Domain("pass.agents")})
    first = Step("page relay", lambda ctx: Result(), group="relay", writes=values)
    seen: list[Context] = []

    def call(ctx: Context) -> Result:
        seen.append(ctx)
        return Result()

    later = Step("consumer", call, frozenset({"declared"}), group="report", reads=values)
    edged = registry.site_edges((first, later), ("relay", "report"))
    assert later.needs == frozenset({"declared"})
    assert edged[1].needs == frozenset({"declared", "page relay"})
    assert registry.failure_needs(later) == frozenset({"declared"})
    assert registry.failure_needs(edged[1]) == frozenset({"declared"})
    again = registry.site_edges(edged, ("relay", "report"))
    assert again[1].needs == edged[1].needs
    assert registry.failure_needs(again[1]) == frozenset({"declared"})
    assert again[1].name == "consumer"
    assert again[1].group == later.group
    assert again[1].lane == later.lane
    assert again[1].budget_s == later.budget_s
    assert again[1].interval_s == later.interval_s
    assert again[1].reads == later.reads
    assert again[1].writes == later.writes
    assert again[1].effects == later.effects
    assert again[1].feature == later.feature
    setattr(later, "placing", True)
    assert getattr(again[1], "placing") is True
    ctx = context()
    assert again[1].run(ctx) == Result()
    assert seen == [ctx]


def test_only_declared_needs_propagate_dropped_cut_and_raised_steps() -> None:
    values = frozenset({Domain("pass.agents")})
    first = Step("page relay", lambda ctx: Result(), group="relay", writes=values)
    ordered = Step("ordered", lambda ctx: Result(), group="report", reads=values)
    declared = Step("declared", lambda ctx: Result(), frozenset({"page relay"}), group="report")
    after = Step("after", lambda ctx: Result(), frozenset({"declared"}), group="report")
    _, ordered, declared, after = registry.site_edges((first, ordered, declared, after), ("relay", "report"))
    assert engine._gone((ordered, declared, after), frozenset({"page relay"})) == frozenset({"declared", "after"})
    with pool.running(critical_s=10.0, reserves={}) as found:
        dropped = {"page relay"}
        cut = {"page relay"}
        lane = Deadline.after(10.0)
        assert not engine._drops(ordered, dropped, found)
        assert engine._drops(declared, dropped, found)
        assert engine._cut_off(ordered, lane, (dropped, set()), found, {}) is None
        assert engine._cut_off(ordered, lane, (set(), cut), found, {}) is None
        assert engine._cut_off(declared, lane, (dropped, set()), found, {}) is dropped
        assert engine._cut_off(declared, lane, (set(), cut), found, {"declared": 1.0}) is cut
    fallen = {"page relay"}
    box = {step_module.FALLEN: fallen}
    assert not step_module.fell(box, ordered)
    assert step_module.fell(box, declared)
    assert step_module.fell(box, after)
    assert fallen == {"page relay", "declared", "after"}


def test_pool_waits_for_ordering_events_but_checks_only_required_failures() -> None:
    with pool.running() as found:
        found.track({"page relay", "declared", "waiting"})
        found.end("page relay", True)
        found.end("declared", False)
        assert not found.ended({"page relay", "declared"}, 0.0)
        assert found.ended({"page relay", "declared"}, 0.0, required={"declared"})
        assert found.ended({"page relay"}, 0.0, required=())
        assert not found.ended({"waiting"}, 0.0, required=())
