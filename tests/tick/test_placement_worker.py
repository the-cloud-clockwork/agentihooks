"""Steps that take the host placement lock run one at a time on the pass's one placement worker, under that lock, so
a step waiting for the lock never holds one of the pass's shared step workers."""

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, Lock, current_thread
from time import sleep
from typing import Any

import pytest

from scripts.tick import engine, pool, registry
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain, Lane, Result
from scripts.tick.steps import CallStep
from scripts.tick.steps.tasks import WriteStep

pytestmark = pytest.mark.unit

GROUP = "placement"
PLACING_STEPS = frozenset(
    {
        "quota notice",
        "codex hook order",
        "rank pass",
        "master down",
        "tick master",
        "master scale",
        "seat alarm",
        "dispatch seat",
        "expert seats",
    }
)


class View:
    def read(self, domain: Domain, key: str) -> object | None:
        return None


@dataclass
class Step:
    name: str
    action: Callable[[Context], Result]
    group: str
    needs: frozenset[str] = frozenset()
    lane: Lane = Lane("critical")
    budget_s: float = 5.0
    interval_s: float = 0.0
    reads: frozenset[Domain] = frozenset()
    writes: frozenset[Domain] = frozenset()
    effects: frozenset[str] = frozenset()
    feature: str = ""
    placing: bool = False
    kept: bool = False

    def run(self, ctx: Context) -> Result:
        return self.action(ctx)


class HostLock:
    def __init__(self) -> None:
        self.held = False
        self.entered = 0

    def __enter__(self) -> "HostLock":
        self.held = True
        self.entered += 1
        return self

    def __exit__(self, *exc: object) -> None:
        self.held = False


def _ctx() -> Context:
    return Context(View(), Deadline.after(10.0))


def _quick(ctx: Context) -> Result:
    return Result()


@pytest.mark.feature("placement-lock-steps-on-one-placement-worker")
def test_a_step_ready_beside_more_lock_takers_than_workers_starts_while_they_wait_for_the_lock() -> None:
    gate, taken = Event(), Lock()

    def holder(ctx: Context) -> Result:
        with taken:
            assert gate.wait(3), "the ordinary step never got a worker while the lock takers waited"
        return Result()

    def waiter(ctx: Context) -> Result:
        with taken:
            return Result()

    def free(ctx: Context) -> Result:
        gate.set()
        return Result()

    steps = (
        Step("placing holder", holder, GROUP, placing=True),
        Step("placing waiter one", waiter, GROUP, placing=True),
        Step("placing waiter two", waiter, GROUP, placing=True),
        Step("placing free", free, GROUP),
    )
    with pool.running(workers=2, critical_s=8.0, slow_s=8.0):
        rows = engine.run_group(GROUP, _ctx(), steps)

    assert {record.name: record.outcome for record in rows} == {step.name: engine.OK for step in steps}


@pytest.mark.feature("placement-lock-steps-on-one-placement-worker")
def test_a_kept_lock_taking_step_runs_on_the_placement_worker_and_leaves_the_step_worker_free() -> None:
    gate = Event()

    def holder(ctx: Context) -> Result:
        assert gate.wait(3), "the ordinary step never got the pass's one step worker"
        return Result()

    def free(ctx: Context) -> Result:
        gate.set()
        return Result()

    steps = (Step("placing kept", holder, GROUP, placing=True, kept=True), Step("placing free", free, GROUP))
    with pool.running(workers=1, critical_s=8.0, slow_s=8.0):
        rows = engine.run_group(GROUP, _ctx(), steps)

    assert {record.name: record.outcome for record in rows} == {step.name: engine.OK for step in steps}


@pytest.mark.feature("placement-lock-steps-on-one-placement-worker")
def test_lock_taking_steps_of_a_pass_run_one_at_a_time_on_its_one_placement_worker() -> None:
    guard = Lock()
    running: list[int] = [0]
    peak: list[int] = [0]

    def placed(ctx: Context) -> Result:
        with guard:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        sleep(0.1)
        with guard:
            running[0] -= 1
        return Result()

    steps = tuple(Step(f"placing {index}", placed, GROUP, placing=True) for index in range(3))
    with pool.running(workers=3):
        rows = engine.run_group(GROUP, _ctx(), steps)

    assert peak == [1]
    assert [record.outcome for record in rows] == [engine.OK] * 3


@pytest.mark.feature("kept-placing-steps-first-on-the-placement-worker")
def test_a_kept_lock_taking_step_ready_beside_a_slower_one_ahead_of_it_runs_first_on_the_placement_worker() -> None:
    order: list[str] = []
    seated = Event()

    def notice(ctx: Context) -> Result:
        seated.wait(10)
        order.append("notice")
        return Result()

    def seat(ctx: Context) -> Result:
        order.append("seat")
        seated.set()
        return Result()

    steps = (
        Step("placing notice", notice, GROUP, placing=True, budget_s=15.0),
        Step("placing seat", seat, GROUP, placing=True, kept=True),
    )
    with pool.running(workers=2, critical_s=20.0, slow_s=20.0):
        rows = engine.run_group(GROUP, Context(View(), Deadline.after(20.0)), steps)

    assert order == ["seat", "notice"]
    assert {record.name: record.outcome for record in rows} == {step.name: engine.OK for step in steps}


@pytest.mark.feature("kept-placing-steps-first-on-the-placement-worker")
def test_the_kept_placing_steps_of_the_placement_site_reach_the_placement_worker_ahead_of_quota_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = (
        "quota notice",
        "codex hook order",
        "master down",
        "tick master",
        "master scale",
        "seat alarm",
        "dispatch seat",
        "rank pass",
    )
    registered = {step.name: step for wave in registry.waves() for step in wave}
    started: list[str] = []

    def starts(name: str) -> Callable[[Context], Result]:
        def run(ctx: Context) -> Result:
            started.append(name)
            return Result()

        return run

    for name in site:
        monkeypatch.setitem(registered[name].__dict__, "run", starts(name))
    with pool.running(workers=2, critical_s=20.0, slow_s=20.0):
        rows = engine.run_graph((registered[name] for name in site), Context(View(), Deadline.after(20.0)))

    assert set(started[:6]) == {
        "codex hook order",
        "master down",
        "tick master",
        "master scale",
        "seat alarm",
        "rank pass",
    }
    assert started[6:] == ["quota notice", "dispatch seat"]
    assert {record.name: record.outcome for record in rows} == dict.fromkeys(site, engine.OK)


@pytest.mark.feature("placement-lock-steps-on-one-placement-worker")
def test_a_lock_taking_step_on_the_slow_lane_runs_on_the_placement_worker() -> None:
    order: list[str] = []
    gate, started, critical = Event(), Event(), Event()

    def slow(ctx: Context) -> Result:
        order.append("slow started")
        started.set()
        gate.wait(3)
        order.append("slow ended")
        return Result()

    def placed(ctx: Context) -> Result:
        order.append("critical ran")
        critical.set()
        return Result()

    def free(ctx: Context) -> Result:
        critical.wait(0.5)
        gate.set()
        return Result()

    later: list[tuple[engine.StepRecord, ...]] = []
    with pool.running(workers=2, critical_s=6.0, slow_s=6.0):
        engine.run_group(
            GROUP,
            _ctx(),
            (Step("placing slow", slow, GROUP, lane=Lane("slow"), placing=True),),
            finished=later.append,
        )
        assert started.wait(3)
        rows = engine.run_group(
            "plain",
            _ctx(),
            (Step("placing critical", placed, "plain", placing=True), Step("plain free", free, "plain")),
        )

    assert order == ["slow started", "slow ended", "critical ran"]
    assert [record.outcome for row in (*later, rows) for record in row] == [engine.OK] * 3


def test_a_step_runs_on_the_placement_worker_only_when_it_declares_the_placement_lock() -> None:
    assert engine._placing(Step("placing declared", _quick, GROUP, placing=True)) is True
    assert engine._placing(Step("placing not declared", _quick, GROUP)) is False
    assert engine._placing(object()) is False  # pyright: ignore[reportArgumentType]


def test_a_pass_runs_an_ordinary_step_on_its_step_workers_and_shuts_its_placement_worker_down() -> None:
    with pool.running(workers=2) as found:
        assert found.executor("plain step", False) is found.steps
        assert found.executor("plain step", True) is found.placer
        assert found.placer.submit(lambda: current_thread().name).result().startswith("tick-placing_")
    with pytest.raises(RuntimeError):
        found.placer.submit(lambda: 1)


def test_a_pass_cancels_lock_taking_steps_still_queued_on_its_placement_worker_when_it_ends() -> None:
    gate, started, finished = Event(), Event(), Event()

    def blocker() -> None:
        started.set()
        gate.wait(5)
        finished.set()

    try:
        with pool.running(workers=1) as found:
            found.placer.submit(blocker)
            assert started.wait(5)
            queued = found.placer.submit(lambda: None)
        assert not finished.is_set(), "the pass end waited for the running lock taking step"
        assert queued.cancelled()
    finally:
        gate.set()


def _tick_lock(monkeypatch: pytest.MonkeyPatch) -> HostLock:
    from scripts.swarm import tick

    lock = HostLock()
    monkeypatch.setattr(tick, "PLACING", lock)
    return lock


@pytest.mark.feature("placement-lock-steps-on-one-placement-worker")
def test_a_call_step_that_declares_the_placement_lock_runs_its_call_holding_it(monkeypatch: pytest.MonkeyPatch) -> None:
    lock = _tick_lock(monkeypatch)
    seen: list[bool] = []

    def call(tick: Any, get: Callable[[str], Any]) -> list[str]:
        seen.append(tick.PLACING.held)
        return ["placed"]

    placed = CallStep("quota notice", GROUP, call, placing=True)
    result = placed.run(_ctx())

    assert placed.placing is True
    assert seen == [True]
    assert (lock.entered, lock.held) == (1, False)
    assert [write.value for write in result.writes] == [["placed"]]


def test_a_call_step_that_declares_no_placement_lock_runs_its_call_without_it(monkeypatch: pytest.MonkeyPatch) -> None:
    lock = _tick_lock(monkeypatch)
    seen: list[bool] = []

    def call(tick: Any, get: Callable[[str], Any]) -> list[str]:
        seen.append(tick.PLACING.held)
        return []

    plain = CallStep("quota notice", GROUP, call)
    plain.run(_ctx())

    assert plain.placing is False
    assert seen == [False]
    assert lock.entered == 0


@pytest.mark.feature("placement-lock-steps-on-one-placement-worker")
def test_a_write_step_that_declares_the_placement_lock_runs_its_call_holding_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = _tick_lock(monkeypatch)
    seen: list[bool] = []

    def call(tick: Any, get: Callable[[str], Any]) -> list[str]:
        seen.append(tick.PLACING.held)
        return ["ranked"]

    placed = WriteStep("rank pass", GROUP, call, placing=True)
    result = placed.run(_ctx())
    plain = WriteStep("rank pass", GROUP, call)
    plain.run(_ctx())

    assert (placed.placing, plain.placing) == (True, False)
    assert seen == [True, False]
    assert (lock.entered, lock.held) == (1, False)
    assert [write.value for write in result.writes] == [["ranked"]]


def _ranked(tick: Any, get: Callable[[str], Any]) -> list[str]:
    return []


@pytest.mark.feature("kept-placing-steps-first-on-the-placement-worker")
def test_a_write_step_is_kept_only_when_it_declares_kept() -> None:
    assert getattr(WriteStep("rank pass", GROUP, _ranked, kept=True), "kept", False) is True
    assert getattr(WriteStep("rank pass", GROUP, _ranked), "kept", False) is False


@pytest.mark.feature("placement-lock-steps-on-one-placement-worker")
def test_the_nine_steps_that_place_agents_declare_the_placement_lock() -> None:
    steps = {step.name: step for wave in registry.waves() for step in wave}

    assert {name for name, step in steps.items() if getattr(step, "placing", False)} == PLACING_STEPS
