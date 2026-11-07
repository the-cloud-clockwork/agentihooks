"""The master seat steps run in every pass, even when an earlier step overran its lane or the intent check overran."""

from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Event
from time import sleep

import pytest

from scripts.tick import engine, pool, registry
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain, Lane, Result
from scripts.tick.steps import host, masters

pytestmark = pytest.mark.unit


class View:
    def read(self, domain: Domain, key: str) -> object | None:
        return None


@dataclass
class Step:
    name: str
    group: str
    action: Callable[[Context], Result] = field(default=lambda ctx: Result())
    needs: frozenset[str] = frozenset()
    lane: Lane = Lane("tick")
    budget_s: float = 5.0
    interval_s: float = 0.0
    reads: frozenset[Domain] = frozenset()
    writes: frozenset[Domain] = frozenset()
    effects: frozenset[str] = frozenset()
    feature: str = ""
    kept: bool = False

    def run(self, ctx: Context) -> Result:
        return self.action(ctx)


def _blocking(release: Event) -> Callable[[Context], Result]:
    def action(ctx: Context) -> Result:
        release.wait(3.0)
        return Result()

    return action


def _sleeping(seconds: float) -> Callable[[Context], Result]:
    def action(ctx: Context) -> Result:
        sleep(seconds)
        return Result()

    return action


def test_the_intent_check_runs_on_the_slow_lane() -> None:
    found = next(step for step in host.STEPS if step.name == "intent check")
    assert found.lane == engine.SLOW


def test_master_down_and_tick_master_are_kept_steps() -> None:
    kept = {step.name for step in masters.STEPS if getattr(step, "kept", False)}
    assert kept == {"master down", "tick master"}


def test_a_kept_step_runs_after_its_lane_expired_and_a_step_it_needs_was_dropped() -> None:
    release = Event()
    steps = (
        Step("overrun", "a", _blocking(release)),
        Step("master down", "b", needs=frozenset({"overrun"}), kept=True),
        Step("other", "b", needs=frozenset({"overrun"})),
    )
    try:
        with pool.running(critical_s=0.3, slow_s=1.0):
            records = {r.name: r for r in engine.run_graph(steps, Context(View(), Deadline.after(10.0)))}
    finally:
        release.set()
    assert records["overrun"].abandoned
    assert records["other"].skipped
    assert records["master down"].outcome == engine.OK


def test_an_overrunning_intent_check_still_lets_the_master_steps_run() -> None:
    release = Event()
    ran: list[str] = []

    def seat(name: str) -> Callable[[Context], Result]:
        def action(ctx: Context) -> Result:
            ran.append(name)
            return Result()

        return action

    intent = next(step for step in host.STEPS if step.name == "intent check")
    steps = (
        Step("intent check", "wrapper intent", _blocking(release), lane=intent.lane, budget_s=60.0),
        Step("reboot pass", "wrapper reboot", _sleeping(0.5)),
        Step("master down", masters.SEATS, seat("master down"), kept=True),
        Step("tick master", masters.SEATS, seat("tick master"), kept=True),
        Step("page relay", "wrapper relay"),
    )
    sites = ("wrapper intent", "wrapper reboot", masters.SEATS, "wrapper relay")
    edged = registry.site_edges(steps, sites, {"wrapper intent": "wrapper relay"})
    try:
        with pool.running(critical_s=0.3, slow_s=1.0):
            records = {r.name: r for r in engine.run_graph(edged, Context(View(), Deadline.after(10.0)))}
            assert sorted(ran) == ["master down", "tick master"]
    finally:
        release.set()
    assert records["master down"].outcome == engine.OK
    assert records["tick master"].outcome == engine.OK
    assert "intent check" not in records
