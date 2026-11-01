from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from unittest import mock

import pytest

from scripts.tick import engine, registry
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain, Lane, Result

MARK: ContextVar[str] = ContextVar("mark", default="clean")


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class View:
    def read(self, domain: Domain, key: str) -> object | None:
        return None


@dataclass
class FakeStep:
    name: str
    order: int
    action: Callable[[Context], Result]
    group: str = "tasks"
    lane: Lane = Lane("main")
    budget_s: float = 5.0
    interval_s: float = 0.0
    needs: frozenset[str] = frozenset()
    writes: frozenset[Domain] = frozenset()
    seen: list[Context] = field(default_factory=list)

    def run(self, ctx: Context) -> Result:
        self.seen.append(ctx)
        return self.action(ctx)


def done(ctx: Context) -> Result:
    return Result()


def context(clock: Clock, seconds: float = 60.0) -> Context:
    return Context(View(), Deadline.after(seconds, clock=clock))


def test_run_group_runs_group_steps_in_order_and_continues_after_failure_and_exit() -> None:
    clock = Clock()
    ran: list[str] = []

    def note(name: str, error: BaseException | None = None) -> Callable[[Context], Result]:
        def action(ctx: Context) -> Result:
            ran.append(name)
            if error is not None:
                raise error
            return Result()

        return action

    steps = [
        FakeStep("third", 3, note("third")),
        FakeStep("first", 1, note("first", RuntimeError("boom"))),
        FakeStep("other", 0, note("other"), group="place"),
        FakeStep("second", 2, note("second", SystemExit(3))),
    ]
    records = engine.run_group("tasks", context(clock), steps)
    assert ran == ["first", "second", "third"]
    assert [(record.name, record.outcome, record.error) for record in records] == [
        ("first", engine.FAILED, "builtins.RuntimeError"),
        ("second", engine.FAILED, "builtins.SystemExit"),
        ("third", engine.OK, ""),
    ]
    assert records[2].result == Result()


def test_overrun_is_recorded_late_and_next_step_still_runs() -> None:
    clock = Clock()

    def slow(ctx: Context) -> Result:
        clock.now += 7.5
        return Result()

    steps = [FakeStep("slow", 1, slow), FakeStep("after", 2, done)]
    records = engine.run_group("tasks", context(clock), steps)
    assert [(record.name, record.outcome) for record in records] == [("slow", engine.LATE), ("after", engine.OK)]
    assert records[0].wall_s == 7.5
    assert records[1].wall_s == 0.0


def test_step_exactly_on_budget_is_ok() -> None:
    clock = Clock()

    def exact(ctx: Context) -> Result:
        clock.now += 5.0
        return Result()

    assert engine.run_step(FakeStep("exact", 1, exact), context(clock)).outcome == engine.OK


def test_failed_record_carries_wall_time() -> None:
    clock = Clock()

    def fail(ctx: Context) -> Result:
        clock.now += 2.0
        raise ValueError("bad")

    record = engine.run_step(FakeStep("fail", 1, fail), context(clock))
    assert (record.outcome, record.wall_s, record.budget_s, record.error, record.result) == (
        engine.FAILED,
        2.0,
        5.0,
        "builtins.ValueError",
        None,
    )


class StepFault(Exception):
    """A step failure defined outside the builtins module."""


def test_failed_record_names_the_exception_module() -> None:
    def fail(ctx: Context) -> Result:
        raise StepFault

    record = engine.run_step(FakeStep("fail", 1, fail), context(Clock()))
    assert record.error == f"{__name__}.StepFault"


def test_each_step_gets_its_own_budget_deadline_capped_by_the_group() -> None:
    clock = Clock()
    roomy = FakeStep("roomy", 1, done, budget_s=5.0)
    capped = FakeStep("capped", 2, done, budget_s=500.0)
    parent = context(clock, 60.0)
    engine.run_group("tasks", parent, [roomy, capped])
    assert roomy.seen[0].deadline.expires_at == 105.0
    assert capped.seen[0].deadline.expires_at == 160.0
    assert roomy.seen[0].deadline.clock is clock
    assert roomy.seen[0].view is parent.view


def test_context_changes_inside_a_step_do_not_leak() -> None:
    clock = Clock()
    seen: list[str] = []

    def mark(ctx: Context) -> Result:
        MARK.set("dirty")
        return Result()

    def read(ctx: Context) -> Result:
        seen.append(MARK.get())
        return Result()

    engine.run_group("tasks", context(clock), [FakeStep("mark", 1, mark), FakeStep("read", 2, read)])
    assert seen == ["clean"]
    assert MARK.get() == "clean"


def test_step_runs_through_timing_call() -> None:
    clock = Clock()
    step = FakeStep("timed", 1, done)
    with mock.patch.object(engine.timing, "within", return_value=Result()) as call:
        record = engine.run_step(step, context(clock))
    budget, function, ctx = call.call_args.args
    assert budget == 5.0
    assert function == step.run
    assert ctx.deadline.expires_at == 105.0
    assert record.result == Result()


def test_tick_stop_is_not_swallowed() -> None:
    from scripts.swarm import tick_stop

    def stop(ctx: Context) -> Result:
        raise tick_stop.Stopped

    with pytest.raises(tick_stop.Stopped):
        engine.run_step(FakeStep("stop", 1, stop), context(Clock()))


def test_group_steps_reads_registered_steps_by_default() -> None:
    fresh = registry.Registry()
    later = FakeStep("later", 2, done)
    earlier = FakeStep("earlier", 1, done)
    fresh.register(later)
    fresh.register(earlier)
    fresh.register(FakeStep("elsewhere", 0, done, group="close"))
    with mock.patch.object(registry, "registry", fresh):
        assert engine.group_steps("tasks") == (earlier, later)
        assert [record.name for record in engine.run_group("tasks", context(Clock()))] == ["earlier", "later"]


def test_empty_group_runs_nothing() -> None:
    assert engine.run_group("tasks", context(Clock()), []) == ()
