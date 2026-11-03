from collections.abc import Callable
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from pathlib import Path
from threading import Barrier, BrokenBarrierError, Event, Lock, Thread, current_thread, get_ident
from time import monotonic, sleep

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


def test_fan_out_waits_for_running_siblings_before_raising() -> None:
    from scripts.tick import pool

    started, finished = Event(), Event()

    def action(index: int) -> int:
        if index == 0:
            assert started.wait(1)
            raise RuntimeError("first agent failed")
        started.set()
        assert not finished.wait(0.2)
        finished.set()
        return index

    with pool.running(workers=2), pytest.raises(RuntimeError, match="first agent failed"):
        pool.fan_out(action, range(2))
    assert finished.is_set()


def test_concurrent_fan_outs_share_the_pass_worker_bound() -> None:
    from scripts.tick import pool

    lock = Lock()
    active, peak = 0, 0

    def action(index: int) -> int:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        Event().wait(0.1)
        with lock:
            active -= 1
        return index

    with pool.running(workers=2):
        threads = [Thread(target=copy_context().run, args=(pool.fan_out, action, range(4))) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
    assert peak == 2


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

    later: list[tuple[engine.StepRecord, ...]] = []
    with pool.running(critical_s=0.03, slow_s=0.5, heartbeat=beat.set):
        assert (
            engine.run_group("load", context(), (Step("slow", slow, lane=Lane("slow")),), finished=later.append) == ()
        )
    assert beat.is_set()
    assert [[row.outcome for row in records] for records in later] == [[engine.OK]]


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
        assert [row.budget_s for row in rows] == [2.0, 2.0]
        assert 0.0 < rows[0].wall_s < 30.0
        assert rows[1].wall_s == 0.0
    finally:
        release.set()


def test_an_expired_lane_drops_its_ready_step_and_every_dependent_in_another_lane() -> None:
    from scripts.tick import pool

    ran: list[str] = []

    def action(ctx: Context) -> Result:
        ran.append("ran")
        return Result()

    steps = (Step("slow", action, lane=Lane("slow")), Step("after", action, frozenset({"slow"})))
    later: list[tuple[engine.StepRecord, ...]] = []
    with pool.running(slow_s=0.0):
        assert engine.run_group("load", context(), steps, finished=later.append) == ()
    assert ran == []
    assert [(row.name, row.outcome, row.wall_s, row.budget_s) for row in later[0]] == [
        ("slow", engine.LATE, 0.0, 2.0),
        ("after", engine.LATE, 0.0, 2.0),
    ]


def test_queued_steps_of_an_expired_lane_never_start() -> None:
    from scripts.tick import pool

    release, queued = Event(), Event()

    def stuck(ctx: Context) -> Result:
        release.wait(1)
        return Result()

    def later(ctx: Context) -> Result:
        queued.set()
        return Result()

    try:
        with pool.running(workers=1, critical_s=0.02):
            rows = engine.run_group("load", context(), (Step("stuck", stuck), Step("later", later)))
        assert [row.outcome for row in rows] == [engine.LATE, engine.LATE]
    finally:
        release.set()
    assert not queued.wait(0.2)


def test_eager_steps_run_under_the_caller_clock() -> None:
    from scripts.tick import pool

    seen: list[Callable[[], float]] = []

    def clock() -> float:
        return monotonic()

    def action(ctx: Context) -> Result:
        seen.append(ctx.deadline.clock)
        return Result()

    with pool.running(workers=2):
        engine.run_group(
            "load", Context(View(), Deadline.after(5, clock=clock)), (Step("a", action), Step("b", action))
        )
    assert seen == [clock, clock]


def test_eager_refuses_steps_whose_needs_never_end() -> None:
    from scripts.tick import pool

    def action(ctx: Context) -> Result:
        return Result()

    steps = (Step("a", action, frozenset({"b"})), Step("b", action, frozenset({"a"})))
    with pool.running(workers=2) as found, pytest.raises(ValueError) as raised:
        engine._eager(steps, context(), found, {})
    assert str(raised.value) == "steps ['a', 'b'] wait on needs that never end"


def test_an_abandoned_step_keeps_running_after_its_group_returns() -> None:
    from scripts.tick import pool

    release, finished = Event(), Event()

    def stuck(ctx: Context) -> Result:
        release.wait(1)
        finished.set()
        return Result()

    try:
        with pool.running(critical_s=0.02):
            rows = engine.run_group("load", context(), (Step("stuck", stuck),))
            assert not finished.is_set()
        assert [row.outcome for row in rows] == [engine.LATE]
    finally:
        release.set()


def test_a_fan_out_inside_a_fanned_out_action_runs_inline() -> None:
    from scripts.tick import pool

    def inner(value: str) -> str:
        return current_thread().name

    def outer(index: int) -> tuple[str, list[str]]:
        return current_thread().name, pool.fan_out(inner, ("a", "b"))

    with pool.running(workers=4):
        found = pool.fan_out(outer, range(2))
    assert [names for _, names in found] == [[name, name] for name, _ in found]


def test_the_heartbeat_timer_runs_on_a_daemon_thread() -> None:
    from scripts.tick import pool

    daemon: list[bool] = []
    beat = Event()

    def heartbeat() -> None:
        daemon.append(current_thread().daemon)
        beat.set()

    with pool.running(critical_s=0.01, heartbeat=heartbeat):
        assert beat.wait(1)
    assert daemon == [True]


def test_a_fresh_host_lock_is_not_held(tmp_path: Path) -> None:
    from scripts.tick import pool

    lock = pool.HostLock(tmp_path / "placing.lock")
    assert not lock.locked()
    with lock:
        assert lock.locked()
    assert not lock.locked()


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


@pytest.mark.wall_clock
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


def test_host_placement_lock_serializes_distinct_lock_instances(tmp_path: Path) -> None:
    from scripts.tick import pool

    entered, release, second = Event(), Event(), Event()
    path = tmp_path / "placing.lock"

    def first() -> None:
        with pool.HostLock(path):
            entered.set()
            release.wait(1)

    def next_host() -> None:
        with pool.HostLock(path):
            second.set()

    first_worker = Thread(target=first)
    first_worker.start()
    assert entered.wait(1)
    next_worker = Thread(target=next_host)
    next_worker.start()
    try:
        assert not second.wait(0.02)
    finally:
        release.set()
        first_worker.join(1)
        next_worker.join(1)
    assert second.is_set()


def test_running_rejects_an_unknown_mode() -> None:
    from scripts.tick import pool

    with pytest.raises(ValueError) as raised:
        with pool.running(mode="turbo"):
            pass
    assert str(raised.value) == "tick mode must be one of parallel, sequential"


def test_running_rejects_zero_workers() -> None:
    from scripts.tick import pool

    with pytest.raises(ValueError) as raised:
        with pool.running(workers=0):
            pass
    assert str(raised.value) == "a pass needs at least one worker"


def test_host_lock_nested_with_a_callable_path_creates_the_missing_parent_and_releases(tmp_path: Path) -> None:
    from scripts.tick import pool

    parent = tmp_path / "missing" / "deeper"
    paths = iter([parent / "a.lock", parent / "b.lock", parent / "c.lock"])
    lock = pool.HostLock(lambda: next(paths))
    assert not parent.exists()
    assert not lock.locked()
    with lock:
        assert lock.locked()
        with lock:
            assert lock.locked()
        assert lock.locked()
    assert not lock.locked()
    assert parent.is_dir()
    assert (parent / "a.lock").exists()
    assert (parent / "b.lock").exists()
    with lock:
        assert lock.locked()
    assert not lock.locked()


def test_fan_out_thread_names_and_worker_count_for_four_values() -> None:
    from scripts.tick import pool

    barrier = Barrier(2)
    lock = Lock()
    names: set[str] = set()

    def action(value: int) -> int:
        with lock:
            names.add(current_thread().name)
        try:
            barrier.wait(timeout=2)
        except BrokenBarrierError:
            pytest.fail("fan_out did not run exactly two concurrent workers")
        return value

    with pool.running(workers=2):
        found = pool.fan_out(action, range(4))
    assert found == list(range(4))
    assert len(names) == 2
    assert all(name.startswith("tick-fan-out") for name in names)


def test_fan_out_returns_at_once_and_cancels_queued_items_when_waiting_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.tick import pool

    gate, lock = Event(), Lock()
    ran: list[int] = []
    finished: list[int] = []

    def action(value: int) -> int:
        with lock:
            ran.append(value)
        gate.wait(timeout=2)
        with lock:
            finished.append(value)
        return value

    def failing_wait(futures: object) -> None:
        raise RuntimeError("wait failed")

    monkeypatch.setattr(pool, "wait", failing_wait)
    with pytest.raises(RuntimeError, match="wait failed"), pool.running(workers=2):
        pool.fan_out(action, range(4))
    assert finished == []
    gate.set()
    for _ in range(10):
        if len(ran) == 4:
            break
        sleep(0.05)
    assert set(ran) <= {0, 1}


def test_fan_out_runs_in_the_caller_thread_outside_a_pass() -> None:
    from scripts.tick import pool

    caller = get_ident()
    seen = pool.fan_out(lambda _: get_ident(), range(3))
    assert seen == [caller, caller, caller]


def test_fan_out_runs_in_the_caller_thread_inside_a_sequential_pass() -> None:
    from scripts.tick import pool

    caller = get_ident()
    with pool.running(mode="sequential"):
        seen = pool.fan_out(lambda _: get_ident(), range(3))
    assert seen == [caller, caller, caller]


def test_fan_out_action_sees_the_caller_context_var_and_preserves_order() -> None:
    from scripts.tick import pool

    marker: ContextVar[str] = ContextVar("order_marker", default="missing")
    marker.set("caller-value")

    def action(value: int) -> tuple[int, str]:
        return value, marker.get()

    with pool.running(workers=4):
        found = pool.fan_out(action, range(6))
    assert found == [(value, "caller-value") for value in range(6)]


def test_eager_step_reads_the_caller_context_var_set_before_the_pass() -> None:
    from scripts.tick import pool

    marker: ContextVar[str] = ContextVar("eager_marker", default="missing")
    marker.set("caller-value")
    seen: list[str] = []

    def action(ctx: Context) -> Result:
        seen.append(marker.get())
        return Result()

    with pool.running(workers=2):
        records = engine.run_group("load", context(), (Step("a", action), Step("b", action)))
    assert seen == ["caller-value", "caller-value"]
    assert [row.name for row in records] == ["a", "b"]


def test_a_group_returns_its_critical_steps_while_its_slow_step_still_runs() -> None:
    from scripts.tick import pool

    release, critical = Event(), Event()

    def stuck(ctx: Context) -> Result:
        assert release.wait(1)
        return Result()

    def quick(ctx: Context) -> Result:
        critical.set()
        return Result()

    steps = (
        Step("stuck", stuck, lane=Lane("slow")),
        Step("needs stuck", quick, frozenset({"stuck"})),
        Step("quick", quick),
    )
    later: list[tuple[engine.StepRecord, ...]] = []
    with pool.running(workers=3):
        rows = engine.run_group("load", context(), steps, finished=later.append)
        assert [(row.name, row.outcome) for row in rows] == [("quick", engine.OK)]
        assert [row.name for row in engine.run_group("next", context(), (Step("next", quick, group="next"),))] == [
            "next"
        ]
        assert later == []
        release.set()
    assert [[(row.name, row.outcome) for row in records] for records in later] == [
        [("stuck", engine.OK), ("needs stuck", engine.OK)]
    ]


@pytest.mark.wall_clock
def test_the_pass_waits_for_a_hung_slow_step_only_until_the_slow_deadline() -> None:
    from scripts.tick import pool

    release = Event()

    def hung(ctx: Context) -> Result:
        release.wait(5)
        return Result()

    later: list[tuple[engine.StepRecord, ...]] = []
    try:
        with pool.running(slow_s=0.05):
            assert (
                engine.run_group("load", context(), (Step("hung", hung, lane=Lane("slow")),), finished=later.append)
                == ()
            )
        assert [[row.outcome for row in records] for records in later] == [[engine.LATE]]
    finally:
        release.set()


def test_a_sequential_pass_runs_slow_steps_in_the_group() -> None:
    from scripts.tick import pool

    later: list[tuple[engine.StepRecord, ...]] = []
    with pool.running(mode="sequential"):
        rows = engine.run_group(
            "load", context(), (Step("slow", lambda ctx: Result(), lane=Lane("slow")),), later.append
        )
    assert ([row.name for row in rows], later) == (["slow"], [])


def test_a_handed_off_step_starts_after_the_critical_step_it_needs() -> None:
    from scripts.tick import pool

    ran: list[str] = []

    def note(name: str) -> Callable[[Context], Result]:
        def action(ctx: Context) -> Result:
            ran.append(name)
            return Result()

        return action

    steps = (
        Step("critical", note("critical")),
        Step("slow", note("slow"), lane=Lane("slow")),
        Step("both", note("both"), frozenset({"critical", "slow"})),
    )
    later: list[tuple[engine.StepRecord, ...]] = []
    with pool.running(workers=3):
        assert [row.name for row in engine.run_group("load", context(), steps, finished=later.append)] == ["critical"]
    assert ran == ["critical", "slow", "both"]
    assert [[row.name for row in records] for records in later] == [["slow", "both"]]
