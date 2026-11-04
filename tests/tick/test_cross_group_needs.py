"""Parallel tick passes start a step only after the steps it needs that an earlier group of the pass handed off."""

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from threading import Event, Timer

import pytest

from scripts.tick import engine, pool
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain, Lane, Result

pytestmark = pytest.mark.unit

SLOW = Lane("slow")


class View:
    def read(self, domain: Domain, key: str) -> object | None:
        return None


@dataclass
class Step:
    name: str
    action: Callable[[Context], Result]
    group: str
    needs: frozenset[str] = frozenset()
    lane: Lane = SLOW
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


def _blocking(release: Event, ended: Event) -> Callable[[Context], Result]:
    def action(ctx: Context) -> Result:
        try:
            release.wait(5.0)
        finally:
            ended.set()
        return Result()

    return action


def _noting(ran: list[str], name: str) -> Callable[[Context], Result]:
    def action(ctx: Context) -> Result:
        ran.append(name)
        return Result()

    return action


def _seeing(ended: Event, started: Event, seen: list[bool]) -> Callable[[Context], Result]:
    def action(ctx: Context) -> Result:
        seen.append(ended.is_set())
        started.set()
        return Result()

    return action


def test_a_slow_step_starts_only_after_the_slow_step_of_an_earlier_group_it_needs() -> None:
    release, ended, started = Event(), Event(), Event()
    seen: list[bool] = []
    findings = Step("cross health findings", _blocking(release, ended), "cross events")
    record = Step("cross metrics record", _seeing(ended, started, seen), "cross metrics", frozenset({findings.name}))
    later: list[tuple[engine.StepRecord, ...]] = []
    try:
        with pool.running(workers=4, critical_s=0.0, slow_s=3.0):
            assert engine.run_group("cross events", _ctx(), (findings,), finished=later.append) == ()
            assert engine.run_group("cross metrics", _ctx(), (record,), finished=later.append) == ()
            assert not started.wait(0.2)
            release.set()
    finally:
        release.set()
    assert seen == [True]
    assert sorted((row.name, row.outcome) for records in later for row in records) == [
        ("cross health findings", engine.OK),
        ("cross metrics record", engine.OK),
    ]


def test_a_critical_step_that_needs_a_handed_off_step_of_an_earlier_group_is_handed_off_after_it() -> None:
    release, ended, started = Event(), Event(), Event()
    seen: list[bool] = []
    findings = Step("cross slow findings", _blocking(release, ended), "cross slow")
    wake = Step(
        "cross critical wake", _seeing(ended, started, seen), "cross critical", frozenset({findings.name}), Lane("tick")
    )
    later: list[tuple[engine.StepRecord, ...]] = []
    try:
        with pool.running(workers=4, critical_s=3.0, slow_s=3.0):
            engine.run_group("cross slow", _ctx(), (findings,), finished=later.append)
            assert engine.run_group("cross critical", _ctx(), (wake,), finished=later.append) == ()
            release.set()
    finally:
        release.set()
    assert seen == [True]
    assert [[row.name for row in records] for records in later] == [["cross slow findings"], ["cross critical wake"]]


def test_a_step_is_dropped_when_the_handed_off_step_of_an_earlier_group_it_needs_ran_past_the_slow_deadline() -> None:
    release, ended = Event(), Event()
    ran: list[str] = []
    stuck = Step("cross stuck step", _blocking(release, ended), "cross stuck")
    dependent = Step("cross dropped dependent", _noting(ran, "dependent"), "cross dependent", frozenset({stuck.name}))
    later: list[tuple[engine.StepRecord, ...]] = []
    try:
        with pool.running(workers=4, slow_s=0.1):
            engine.run_group("cross stuck", _ctx(), (stuck,), finished=later.append)
            engine.run_group("cross dependent", _ctx(), (dependent,), finished=later.append)
    finally:
        release.set()
        assert ended.wait(5.0)
    assert ran == []
    assert sorted((row.name, row.abandoned) for records in later for row in records) == [
        ("cross dropped dependent", True),
        ("cross stuck step", True),
    ]
    assert [row.wall_s > 0.0 for records in later for row in records if row.name == stuck.name] == [True]


def test_a_step_runs_once_the_handed_off_step_of_an_earlier_group_it_needs_was_held() -> None:
    release, ended, started = Event(), Event(), Event()
    seen: list[bool] = []
    findings = Step("cross held findings", _blocking(release, ended), "cross held", budget_s=0.05)
    record = Step("cross after held", _seeing(ended, started, seen), "cross after", frozenset({findings.name}))
    later: list[tuple[engine.StepRecord, ...]] = []
    try:
        assert engine.run_step(findings, _ctx()).outcome == engine.LATE
        with pool.running(workers=4, critical_s=3.0, slow_s=3.0):
            engine.run_group("cross held", _ctx(), (findings,), finished=later.append)
            engine.run_group("cross after", _ctx(), (record,), finished=later.append)
            assert started.wait(5.0)
    finally:
        release.set()
        assert ended.wait(5.0)
    assert seen == [False]
    assert sorted((row.name, row.outcome) for records in later for row in records) == [
        ("cross after held", engine.OK),
        ("cross held findings", engine.HELD),
    ]


def test_a_sequential_pass_runs_a_cross_group_dependent_after_the_step_it_needs() -> None:
    ran: list[str] = []
    note = partial(_noting, ran)

    first = Step("cross sequential first", note("first"), "cross sequential one")
    second = Step("cross sequential second", note("second"), "cross sequential two", frozenset({first.name}))
    with pool.running(mode="sequential"):
        engine.run_group("cross sequential one", _ctx(), (first,))
        engine.run_group("cross sequential two", _ctx(), (second,))
    assert ran == ["first", "second"]


def test_a_step_is_dropped_when_the_handed_off_step_of_an_earlier_group_it_needs_lost_its_critical_need() -> None:
    ran: list[str] = []
    note = partial(_noting, ran)
    lost = Step("cross lost critical", note("critical"), "cross lost", lane=Lane("tick"))
    slow = Step("cross lost slow", note("slow"), "cross lost", frozenset({lost.name}))
    dependent = Step("cross lost dependent", note("dependent"), "cross after lost", frozenset({slow.name}))
    later: list[tuple[engine.StepRecord, ...]] = []
    with pool.running(workers=4, critical_s=0.0) as found:
        engine.run_group("cross lost", _ctx(), (lost, slow), finished=later.append)
        assert found.pending() == frozenset()
        assert not found.ended({slow.name}, 0.0)
        engine.run_group("cross after lost", _ctx(), (dependent,), finished=later.append)
    assert ran == []
    assert sorted((row.name, row.abandoned) for records in later for row in records) == [
        ("cross lost dependent", True),
        ("cross lost slow", True),
    ]


def test_a_step_is_dropped_when_the_handed_off_step_of_an_earlier_group_it_needs_ran_past_its_budget() -> None:
    release, ended = Event(), Event()
    ran: list[str] = []
    stuck = Step("cross over budget", _blocking(release, ended), "cross budget", budget_s=0.05)
    dependent = Step("cross budget dependent", _noting(ran, "dependent"), "cross budget after", frozenset({stuck.name}))
    later: list[tuple[engine.StepRecord, ...]] = []
    try:
        with pool.running(workers=4, critical_s=3.0, slow_s=3.0):
            engine.run_group("cross budget", _ctx(), (stuck,), finished=later.append)
            engine.run_group("cross budget after", _ctx(), (dependent,), finished=later.append)
    finally:
        release.set()
        assert ended.wait(5.0)
    assert ran == []
    assert sorted((row.name, row.outcome, row.abandoned) for records in later for row in records) == [
        ("cross budget dependent", engine.LATE, True),
        ("cross over budget", engine.LATE, True),
    ]


def test_a_critical_step_runs_in_its_group_once_the_handed_off_step_it_needs_already_ended() -> None:
    ran: list[str] = []
    first = Step("cross ended first", _noting(ran, "first"), "cross ended")
    wake = Step("cross ended wake", _noting(ran, "wake"), "cross ended wake", frozenset({first.name}), Lane("tick"))
    with pool.running(workers=4, critical_s=3.0, slow_s=3.0) as found:
        engine.run_group("cross ended", _ctx(), (first,))
        assert found.handed[first.name].wait(3.0)
        assert found.pending() == frozenset()
        rows = engine.run_group("cross ended wake", _ctx(), (wake,))
    assert [(row.name, row.outcome) for row in rows] == [("cross ended wake", engine.OK)]
    assert ran == ["first", "wake"]


def test_a_critical_step_is_dropped_in_its_group_when_the_handed_off_step_it_needs_was_dropped() -> None:
    ran: list[str] = []
    first = Step("cross gone first", _noting(ran, "first"), "cross gone")
    wake = Step("cross gone wake", _noting(ran, "wake"), "cross gone wake", frozenset({first.name}), Lane("tick"))
    with pool.running(workers=4, critical_s=3.0, slow_s=3.0) as found:
        found.hand((first.name,))
        found.end(first.name, True)
        rows = engine.run_group("cross gone wake", _ctx(), (wake,))
    assert rows == (engine.StepRecord("cross gone wake", 0.0, 5.0, abandoned=True),)
    assert ran == []


def test_a_pass_reports_a_handed_off_step_ended_only_once_it_was_marked_in_time() -> None:
    with pool.running(workers=1) as found:
        found.hand(("cross marked late",))
        timer = Timer(0.2, found.end, ("cross marked late", False))
        timer.start()
        try:
            assert not found.ended({"cross marked late"}, 0.0)
        finally:
            timer.join(5.0)
        assert found.ended({"cross marked late"}, 0.0)
        assert found.pending() == frozenset()


def test_a_pass_ignores_the_end_of_a_step_it_never_handed_off() -> None:
    with pool.running(workers=1) as found:
        found.hand(("cross handed",))
        found.end("cross never handed", True)
        assert found.pending() == frozenset({"cross handed"})
        assert found.handed_of(("cross handed", "cross never handed")) == frozenset({"cross handed"})
        found.end("cross handed", False)
        assert found.pending() == frozenset()
        assert found.gone == set()
