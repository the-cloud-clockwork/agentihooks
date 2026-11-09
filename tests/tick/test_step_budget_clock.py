"""A tick step's budget clock starts when its run starts, and only a run past its own budget is abandoned."""

import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from scripts.swarm import timing
from scripts.tick import engine, pool
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain
from scripts.tick.steps.tasks import WriteStep

pytestmark = pytest.mark.unit

ABANDONED = "swarm_tick_step_abandoned"


class Redis:
    def xadd(self, key: str, fields: dict[str, str], **kwargs: Any) -> None:
        return None

    def hset(self, key: str, field: str, value: str) -> None:
        return None

    def hgetall(self, key: str) -> dict[str, str]:
        return {}

    def pipeline(self, transaction: bool = True) -> "Redis":
        return self

    def lpush(self, key: str, *values: str) -> None:
        return None

    def ltrim(self, key: str, start: int, end: int) -> None:
        return None

    def execute(self) -> list[Any]:
        return []


class Store:
    def __init__(self) -> None:
        self.redis = Redis()

    def key(self, slug: str, stream: str) -> str:
        return f"{slug}:{stream}"


class View:
    def read(self, domain: Domain, key: str) -> object | None:
        return None


@contextmanager
def recording() -> Iterator[None]:
    current = {"pass_id": "p1", "start_ms": 1000, "end_ms": 61000, "wall_s": 60.0, "failed_steps": []}
    token = timing.PASS.set(current)
    try:
        with timing.tick("sw", Store()):
            yield
    finally:
        timing.PASS.reset(token)


def sleeping(seconds: float, ran: list[str], name: str) -> Callable[[Any, Any], list[object]]:
    def call(tick: Any, get: Any) -> list[object]:
        ran.append(name)
        time.sleep(seconds)
        return []

    return call


def step(name: str, seconds: float, budget_s: float, ran: list[str]) -> WriteStep:
    found = WriteStep(name, "planted", sleeping(seconds, ran, name), writes=frozenset({Domain("tasks")}))
    found.floor_s = budget_s
    return found


def rows(err: str) -> list[dict[str, Any]]:
    found = []
    for line in err.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            found.append(row)
    return found


def abandoned(err: str) -> list[str]:
    return [row["step"] for row in rows(err) if row.get("event") == ABANDONED]


def outcomes(err: str, name: str) -> list[str]:
    return [
        row["outcome"]
        for row in rows(err)
        if row.get("event") == "swarm_tick_step" and row.get("phase") == "finished" and row.get("step") == name
    ]


def joined(*names: str) -> None:
    for name in names:
        worker = engine._RUNNING.get(("sw", name))
        if worker is not None:
            worker.join(5)


@pytest.mark.feature("tick-step-budget-from-start")
def test_a_step_still_queued_behind_a_slow_sibling_at_its_lane_end_is_skipped_and_never_starts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ran: list[str] = []
    steps = [step("slow sibling", 0.6, 10.0, ran), step("queued reap", 0.01, 10.0, ran)]
    with recording(), pool.running(workers=1, critical_s=0.3, reserves={}):
        records = {row.name: row for row in engine.run_group("planted", Context(View(), Deadline.after(60.0)), steps)}
    joined("slow sibling", "queued reap")
    time.sleep(0.3)
    assert records["queued reap"].skipped
    assert ran == ["slow sibling"]
    assert abandoned(capsys.readouterr().err) == []


@pytest.mark.feature("tick-step-budget-from-start")
def test_a_step_that_waits_behind_a_slow_sibling_and_then_runs_quickly_is_not_abandoned(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ran: list[str] = []
    steps = [step("slow sibling", 0.5, 10.0, ran), step("late reap", 0.8, 10.0, ran)]
    with recording(), pool.running(workers=1, critical_s=1.0, reserves={}):
        records = {row.name: row for row in engine.run_group("planted", Context(View(), Deadline.after(60.0)), steps)}
    joined("slow sibling", "late reap")
    err = capsys.readouterr().err
    assert ran == ["slow sibling", "late reap"]
    assert records["late reap"].wall_s < 0.9
    assert abandoned(err) == []
    assert outcomes(err, "late reap") == ["success"]


@pytest.mark.feature("tick-step-budget-from-start")
def test_a_step_whose_own_run_overruns_its_budget_is_abandoned_and_never_reports_success(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ran: list[str] = []
    with recording():
        records = engine.run_group(
            "planted", Context(View(), Deadline.after(60.0)), [step("overrun spawn", 0.6, 0.2, ran)]
        )
    joined("overrun spawn")
    err = capsys.readouterr().err
    assert records[0].abandoned and not records[0].skipped
    assert abandoned(err) == ["overrun spawn"]
    assert outcomes(err, "overrun spawn") == ["abandoned"]


@pytest.mark.feature("tick-step-budget-from-start")
def test_a_step_that_ends_inside_its_budget_reports_success_and_no_abandon(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ran: list[str] = []
    with recording():
        records = engine.run_group(
            "planted", Context(View(), Deadline.after(60.0)), [step("quick reap", 0.01, 5.0, ran)]
        )
    err = capsys.readouterr().err
    assert not records[0].abandoned
    assert abandoned(err) == []
    assert outcomes(err, "quick reap") == ["success"]
