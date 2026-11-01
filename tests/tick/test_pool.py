from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Event, Lock
from time import monotonic

import pytest

from scripts.tick import engine
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain, Lane, Result


class View:
    def read(self, domain: Domain, key: str) -> object | None:
        return None


@dataclass
class Step:
    name: str
    action: Callable[[Context], Result]
    needs: frozenset[str] = frozenset()
    lane: Lane = Lane("critical")
    group: str = "load"
    budget_s: float = 2.0
    interval_s: float = 0.0
    reads: frozenset[Domain] = frozenset()
    writes: frozenset[Domain] = frozenset()

    def run(self, ctx: Context) -> Result:
        return self.action(ctx)


def context() -> Context:
    return Context(View(), Deadline.after(5))


def test_dependents_start_before_an_unrelated_running_step_finishes() -> None:
    from scripts.tick import pool

    dependent = Event()

    def slow(ctx: Context) -> Result:
        assert dependent.wait(1), "ready dependent was held behind the unrelated step"
        return Result()

    def done(ctx: Context) -> Result:
        return Result()

    def after(ctx: Context) -> Result:
        dependent.set()
        return Result()

    steps = (Step("slow", slow), Step("fast", done), Step("after", after, frozenset({"fast"})))
    with pool.running(workers=3):
        records = engine.run_group("load", context(), steps)
    assert dependent.is_set()
    assert [row.name for row in records] == ["slow", "fast", "after"]
    assert all(row.outcome == engine.OK for row in records)


def test_fan_out_copies_context_bounds_workers_and_preserves_results() -> None:
    from scripts.tick import pool

    marker: ContextVar[str] = ContextVar("fan_out_marker", default="missing")
    marker.set("swarm")
    lock, release, filled = Lock(), Event(), Event()
    active, peak = 0, 0

    def action(index: int) -> tuple[int, str]:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 3:
                filled.set()
                release.set()
        assert release.wait(1)
        found = marker.get()
        marker.set("worker")
        with lock:
            active -= 1
        return index, found

    with pool.running(workers=3):
        found = pool.fan_out(action, range(15))
    assert filled.is_set()
    assert peak == 3
    assert found == [(index, "swarm") for index in range(15)]
    assert marker.get() == "swarm"


def test_sequential_mode_retains_order_and_does_not_fan_out() -> None:
    from scripts.tick import pool

    order: list[str] = []

    def action(ctx: Context) -> Result:
        order.append("step")
        return Result()

    with pool.running(mode="sequential"):
        assert pool.fan_out(lambda value: order.append(value), ("a", "b")) == [None, None]
        records = engine.run_group("load", context(), (Step("first", action), Step("second", action)))
    assert order == ["a", "b", "step", "step"]
    assert [row.name for row in records] == ["first", "second"]


def test_critical_heartbeat_does_not_wait_for_slow_lane() -> None:
    from scripts.tick import pool

    beat = Event()

    def slow(ctx: Context) -> Result:
        assert beat.wait(1)
        assert ctx.deadline.remaining <= 0.5
        return Result()

    with pool.running(critical_s=0.03, slow_s=0.5, heartbeat=beat.set):
        records = engine.run_group("load", context(), (Step("slow", slow, lane=Lane("slow")),))
    assert beat.is_set()
    assert records[0].outcome == engine.OK


def test_expired_lane_drops_late_result_and_does_not_start_dependent() -> None:
    from scripts.tick import pool

    release, dependent = Event(), Event()

    def stuck(ctx: Context) -> Result:
        release.wait(1)
        return Result()

    def after(ctx: Context) -> Result:
        dependent.set()
        return Result()

    try:
        with pool.running(critical_s=0.02):
            rows = engine.run_group(
                "load", context(), (Step("stuck", stuck), Step("after", after, frozenset({"stuck"})))
            )
        assert all(row.outcome == engine.LATE for row in rows)
        assert not dependent.is_set()
        assert all(row.result is None for row in rows)
    finally:
        release.set()


@pytest.mark.parametrize(("resource", "cap"), (("herdr", 6), ("github", 4)))
def test_external_calls_obey_the_resource_cap(resource: str, cap: int) -> None:
    from scripts.tick import pool

    lock, release = Lock(), Event()
    active, peak = 0, 0

    def call(index: int) -> int:
        nonlocal active, peak
        with pool.limit(resource):
            with lock:
                active += 1
                peak = max(peak, active)
                if active == cap:
                    release.set()
            assert release.wait(1)
            with lock:
                active -= 1
        return index

    with pool.running(workers=15):
        assert pool.fan_out(call, range(15)) == list(range(15))
    assert peak == cap


def test_fifteen_agent_load_finishes_under_sixty_seconds() -> None:
    from scripts.tick import pool

    ready, lock = Event(), Lock()
    started = 0

    def agent(index: int) -> int:
        nonlocal started
        with lock:
            started += 1
            if started == 15:
                ready.set()
        assert ready.wait(2), "per agent work remained sequential"
        return index

    at = monotonic()
    with pool.running(workers=15):
        assert pool.fan_out(agent, range(15)) == list(range(15))
    assert monotonic() - at < 60
