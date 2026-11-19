from threading import Event

import pytest

from scripts.tick import engine, pool, registry
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
