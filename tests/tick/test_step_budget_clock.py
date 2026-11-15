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
    steps = [step("rank pass", 0.6, 10.0, ran), step("reap", 0.01, 10.0, ran)]
    with recording(), pool.running(workers=1, critical_s=0.3, reserves={}):
        records = {row.name: row for row in engine.run_group("planted", Context(View(), Deadline.after(60.0)), steps)}
    joined("rank pass", "reap")
    time.sleep(0.3)
    assert records["reap"].skipped
    assert ran == ["rank pass"]
    assert abandoned(capsys.readouterr().err) == []


@pytest.mark.feature("tick-step-budget-from-start")
def test_a_step_that_waits_behind_a_slow_sibling_and_then_runs_quickly_is_not_abandoned(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ran: list[str] = []
    steps = [step("grouping release", 0.5, 10.0, ran), step("exits sweep", 0.8, 10.0, ran)]
    with recording(), pool.running(workers=1, critical_s=1.0, reserves={}):
        records = {row.name: row for row in engine.run_group("planted", Context(View(), Deadline.after(60.0)), steps)}
    joined("grouping release", "exits sweep")
    err = capsys.readouterr().err
    assert ran == ["grouping release", "exits sweep"]
    assert records["exits sweep"].wall_s < 0.9
    assert records["exits sweep"].cut and not records["exits sweep"].abandoned
    assert records["exits sweep"].outcome == engine.CUT
    assert [row["step"] for row in rows(err) if row.get("event") == engine.CUT_EVENT] == ["exits sweep"]
    assert abandoned(err) == []
    assert outcomes(err, "exits sweep") == ["success"]


@pytest.mark.feature("tick-step-budget-from-start")
def test_a_step_whose_own_run_overruns_its_budget_is_abandoned_and_never_reports_success(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ran: list[str] = []
    with recording():
        records = engine.run_group("planted", Context(View(), Deadline.after(60.0)), [step("size pass", 0.6, 0.2, ran)])
    joined("size pass")
    err = capsys.readouterr().err
    assert records[0].abandoned and not records[0].skipped
    assert abandoned(err) == ["size pass"]
    assert outcomes(err, "size pass") == ["abandoned"]


@pytest.mark.feature("tick-step-budget-from-start")
def test_a_step_that_ends_inside_its_budget_reports_success_and_no_abandon(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ran: list[str] = []
    with recording():
        records = engine.run_group(
            "planted", Context(View(), Deadline.after(60.0)), [step("intent check", 0.01, 5.0, ran)]
        )
    err = capsys.readouterr().err
    assert not records[0].abandoned
    assert abandoned(err) == []
    assert outcomes(err, "intent check") == ["success"]


@pytest.mark.feature("tick-step-budget-from-start")
def test_a_kept_step_whose_start_the_pass_end_closed_is_skipped_and_never_runs() -> None:
    ran: list[str] = []
    kept = step("tick master", 0.01, 5.0, ran)
    with recording(), pool.running(workers=1) as found:
        start = engine._Start(time.monotonic)
        assert start.close() is None
        record = engine._kept_after(frozenset(), kept, Context(View(), Deadline.after(60.0)), found, start)
    assert record == engine.StepRecord("tick master", 0.0, 5.0, abandoned=True, skipped=True)
    assert ran == []


@pytest.mark.feature("tick-step-budget-from-start")
def test_a_step_is_skipped_and_never_runs_when_a_handed_off_step_of_an_earlier_group_it_needs_never_ends() -> None:
    ran: list[str] = []
    waiting = step("verify", 0.01, 5.0, ran)
    with recording(), pool.running(workers=1, critical_s=5.0) as found:
        found.hand(("track outages",))
        found.end("track outages", True)
        start = engine._Start(time.monotonic)
        record = engine._after(
            frozenset({"track outages"}), waiting, Context(View(), Deadline.after(60.0)), found, start
        )
    assert record == engine.StepRecord("verify", 0.0, 5.0, abandoned=True, skipped=True)
    assert ran == []


@pytest.mark.feature("tick-step-budget-from-start")
def test_a_step_is_skipped_and_never_runs_when_its_lane_already_expired_with_no_earlier_group() -> None:
    ran: list[str] = []
    waiting = step("launch checks", 0.01, 5.0, ran)
    with recording(), pool.running(workers=1, critical_s=0.0) as found:
        start = engine._Start(time.monotonic)
        record = engine._after(frozenset(), waiting, Context(View(), Deadline.after(60.0)), found, start)
    assert record == engine.StepRecord("launch checks", 0.0, 5.0, abandoned=True, skipped=True)
    assert ran == []
