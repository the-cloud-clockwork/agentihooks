"""A tick pass runs every step and every fan out on one step executor and one fan out executor of its own."""

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, Lock, Thread, current_thread
from threading import enumerate as threads

import pytest

from scripts.tick import engine, pool
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain, Lane, Result

pytestmark = pytest.mark.unit

WORKERS = 3
GROUPS = ("executors one", "executors two", "executors three")
POOLED = ("tick-", "ThreadPoolExecutor")


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

    def run(self, ctx: Context) -> Result:
        return self.action(ctx)


def _ctx() -> Context:
    return Context(View(), Deadline.after(10.0))


def _recording(seen: dict[int, Thread], lock: Lock) -> Callable[[Context], Result]:
    def note(value: int) -> int:
        with lock:
            seen.update((id(thread), thread) for thread in threads() if thread.name.startswith(POOLED))
        return value

    def action(ctx: Context) -> Result:
        note(0)
        assert pool.fan_out(note, range(4)) == [0, 1, 2, 3]
        return Result()

    return action


def _group(group: str, seen: dict[int, Thread], lock: Lock) -> tuple[Step, ...]:
    steps = [Step(f"{group} {index}", _recording(seen, lock), group) for index in range(8)]
    if group == GROUPS[-1]:
        steps[-1] = Step(steps[-1].name, steps[-1].action, group, lane=Lane("slow"))
    return tuple(steps)


def test_a_pass_of_three_groups_of_eight_steps_uses_at_most_two_executors_and_the_slow_lane_thread() -> None:
    seen: dict[int, Thread] = {}
    before = set(threads())
    lock = Lock()
    rows: list[tuple[engine.StepRecord, ...]] = []
    with pool.running(workers=WORKERS):
        for group in GROUPS:
            rows.append(engine.run_group(group, _ctx(), _group(group, seen, lock), finished=rows.append))
    names = sorted(thread.name for thread in seen.values() if thread not in before)
    assert len(names) <= 2 * WORKERS + 1, f"{len(names)} pooled threads: {names}"
    assert sum(name.startswith("tick-step") for name in names) <= WORKERS
    assert sum(name.startswith("tick-fan-out") for name in names) <= WORKERS
    assert names.count("tick-slow-lane") == 1
    assert {name.split("_")[0] for name in names} <= {"tick-step", "tick-fan-out", "tick-slow-lane"}
    assert sum(len(row) for row in rows) == 24
    assert all(record.outcome == engine.OK for row in rows for record in row)


def test_a_pass_shuts_both_executors_down_when_it_ends() -> None:
    with pool.running(workers=WORKERS) as found:
        assert found.steps.submit(lambda: 1).result() == 1
        assert found.fans.submit(lambda: 2).result() == 2
    with pytest.raises(RuntimeError):
        found.steps.submit(lambda: 1)
    with pytest.raises(RuntimeError):
        found.fans.submit(lambda: 2)


def test_a_fan_out_inside_a_fanned_out_action_runs_inline() -> None:
    outer: list[str] = []
    inner: list[str] = []

    def nested(value: int) -> int:
        inner.append(current_thread().name)
        return value

    def action(value: int) -> list[int]:
        outer.append(current_thread().name)
        return pool.fan_out(nested, range(2))

    with pool.running(workers=WORKERS):
        assert pool.fan_out(action, range(2)) == [[0, 1], [0, 1]]
    assert set(inner) <= set(outer)


def test_a_step_waiting_on_a_handed_off_step_holds_no_pass_worker() -> None:
    gate = Event()

    def held(ctx: Context) -> Result:
        assert gate.wait(3), "the critical step never got a worker"
        return Result()

    def free(ctx: Context) -> Result:
        gate.set()
        return Result()

    def quick(ctx: Context) -> Result:
        return Result()

    later: list[tuple[engine.StepRecord, ...]] = []
    slow = Lane("slow")
    with pool.running(workers=2, critical_s=4.0, slow_s=4.0):
        engine.run_group(
            "waits one", _ctx(), (Step("waits held", held, "waits one", lane=slow),), finished=later.append
        )
        needing = Step("waits needing", quick, "waits two", frozenset({"waits held"}), slow)
        engine.run_group("waits two", _ctx(), (needing,), finished=later.append)
        rows = engine.run_group("waits three", _ctx(), (Step("waits free", free, "waits three"),))
    assert [record.outcome for record in rows] == [engine.OK]
    assert sorted(record.name for row in later for record in row if record.outcome == engine.OK) == [
        "waits held",
        "waits needing",
    ]
