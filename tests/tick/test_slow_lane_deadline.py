from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, Thread
from unittest.mock import Mock

import pytest

from scripts.tick import engine, pool
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain, Lane, Result
from scripts.tick.steps import budgets


class View:
    def read(self, domain: Domain, key: str) -> object | None:
        return None


@dataclass
class Step:
    name: str
    action: Callable[[Context], Result]
    needs: frozenset[str] = frozenset()
    lane: Lane = Lane("slow")
    group: str = "load"
    floor_s: float = 15.0
    interval_s: float = 0.0
    reads: frozenset[Domain] = frozenset()
    writes: frozenset[Domain] = frozenset()
    effects: frozenset[str] = frozenset()
    feature: str = "swarm-tick-slow-lane-live-budget"

    @property
    def budget_s(self) -> float:
        return budgets.floored(self.name, self.floor_s)

    def run(self, ctx: Context) -> Result:
        return self.action(ctx)


def quick(ctx: Context) -> Result:
    return Result()


@pytest.mark.feature("swarm-tick-slow-lane-live-budget")
def test_slow_deadline_uses_live_budgets_resolved_after_the_pass_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    def detached(current: pool.Pass, work: Callable[[], object]) -> None:
        return None

    monkeypatch.setattr(pool.Pass, "detach", detached)
    token = budgets.LIVE.set({})
    try:
        with pool.running() as current:
            origin = current.slow.expires_at - 59.0
            critical = current.critical.expires_at
            budgets.LIVE.set({"metrics.record": 85.0, "health.findings": 105.0, "report": 20.0})
            ctx = Context(View(), Deadline.after(1000.0))
            engine.run_group("load", ctx, (Step("metrics.record", quick), Step("health.findings", quick)))
            assert current.lane("slow").expires_at - origin == pytest.approx(190.0)
            engine.run_group("report", ctx, (Step("report", quick, group="report"),))
            assert current.lane("slow").expires_at - origin == pytest.approx(210.0)
            assert current.critical.expires_at == critical
    finally:
        budgets.LIVE.reset(token)


@pytest.mark.feature("swarm-tick-slow-lane-live-budget")
def test_a_later_slow_step_finishes_after_the_former_lane_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]

    def clock() -> float:
        return now[0]

    def after(seconds: float) -> Deadline:
        return Deadline(clock() + seconds, clock)

    def earlier(ctx: Context) -> Result:
        now[0] = 58.0
        return Result()

    def later(ctx: Context) -> Result:
        now[0] = 70.0
        ctx.deadline.live()
        return Result()

    monkeypatch.setattr(Deadline, "after", after)
    rows: list[tuple[engine.StepRecord, ...]] = []
    with pool.running(workers=2):
        engine.run_group(
            "load",
            Context(View(), Deadline(1000.0, clock)),
            (Step("earlier", earlier, floor_s=60.0), Step("later", later, frozenset({"earlier"}), floor_s=90.0)),
            finished=rows.append,
        )
    assert [[(row.name, row.outcome) for row in group] for group in rows] == [
        [("earlier", engine.OK), ("later", engine.OK)]
    ]


@pytest.mark.feature("swarm-tick-slow-lane-live-budget")
def test_live_slow_budgets_keep_the_pass_join_inside_its_original_window(monkeypatch: pytest.MonkeyPatch) -> None:
    thread = Mock(spec=Thread)

    def detached(current: pool.Pass, work: Callable[[], object]) -> None:
        current.later.append(thread)

    monkeypatch.setattr(pool.Pass, "detach", detached)
    with pool.running() as current:
        critical = current.critical.expires_at
        engine.run_group("load", Context(View(), Deadline.after(1000.0)), (Step("wide", quick, floor_s=190.0),))
        assert current.critical.expires_at == critical
    assert thread.join.call_count == 1
    assert 0.0 < thread.join.call_args.args[0] <= 59.0 + pool.LATER_GRACE_S


@pytest.mark.feature("swarm-tick-slow-lane-live-budget")
def test_slow_dependents_and_finished_records_survive_the_pass_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    release, started, completed, called = Event(), Event(), Event(), Event()
    rows: list[tuple[engine.StepRecord, ...]] = []

    def earlier(ctx: Context) -> Result:
        started.set()
        assert release.wait(3.0)
        return Result()

    def later(ctx: Context) -> Result:
        called.set()
        return Result()

    def finished(records: tuple[engine.StepRecord, ...]) -> None:
        rows.append(records)
        completed.set()

    monkeypatch.setattr(pool, "SLOW_S", 0.02)
    monkeypatch.setattr(pool, "LATER_GRACE_S", 0.01)
    try:
        with pool.running(workers=2):
            engine.run_group(
                "load",
                Context(View(), Deadline.after(20.0)),
                (
                    Step("across exit", earlier, floor_s=5.0),
                    Step("after exit", later, frozenset({"across exit"}), floor_s=5.0),
                ),
                finished=finished,
            )
            assert started.wait(1.0)
    finally:
        release.set()
    assert completed.wait(3.0)
    assert called.is_set()
    assert [[(row.name, row.outcome) for row in group] for group in rows] == [
        [("across exit", engine.OK), ("after exit", engine.OK)]
    ]
