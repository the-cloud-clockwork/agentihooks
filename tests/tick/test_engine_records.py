"""Engine step records in the tick:records stream and the report columns built from them."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Event
from typing import Any

import pytest

from scripts.swarm import tick_report, timing
from scripts.tick import engine, pool
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain
from scripts.tick.steps.tasks import WriteStep

pytestmark = pytest.mark.unit


class Redis:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, Any]]] = []

    def xadd(self, key: str, fields: dict[str, str], **kwargs: Any) -> None:
        self.rows.append((key, json.loads(fields["record"])))


class Store:
    def __init__(self) -> None:
        self.redis = Redis()

    def key(self, slug: str, stream: str) -> str:
        return f"{slug}:{stream}"

    def records(self) -> list[dict[str, Any]]:
        return [row for key, row in self.redis.rows if key == "sw:tick:records"]


class View:
    def read(self, domain: Domain, key: str) -> object | None:
        return None


@contextmanager
def recording(store: Store) -> Iterator[dict[str, Any]]:
    current = {"pass_id": "p1", "start_ms": 1000, "end_ms": 61000, "wall_s": 60.0, "failed_steps": []}
    token = timing.PASS.set(current)
    try:
        with timing.tick("sw", store):
            yield current
    finally:
        timing.PASS.reset(token)


def planted(release: Event) -> WriteStep:
    step = WriteStep(
        "size pass",
        "planted",
        lambda tick, get: release.wait(30),
        writes=frozenset({Domain("tasks"), Domain("ranks")}),
    )
    step.budget_s = 0.05
    return step


@pytest.mark.feature("swarm-tick-cost-report")
def test_abandoned_write_step_reads_abandoned_with_its_writes_dropped() -> None:
    store, release = Store(), Event()
    try:
        with recording(store) as current:
            engine.run_group("planted", Context(View(), Deadline.after(60.0)), [planted(release)])
            engine.run_group("planted", Context(View(), Deadline.after(60.0)), [planted(release)])
    finally:
        release.set()
        engine._RUNNING[("sw", "size pass")].join(5)
    rows = store.records()
    assert [(row["step"], row["abandoned"], row["held"], row["dropped"]) for row in rows] == [
        ("size pass", True, False, 3),
        ("size pass", False, True, 0),
    ]
    assert rows[0]["pass_id"] == "p1" and rows[0]["outcome"] == engine.LATE and rows[0]["budget_s"] == 0.05
    report = tick_report.summarize([current], [], rows)
    assert report["records"] == [
        {"step": "size pass", "records": 2, "late_share": 50.0, "abandoned": 1, "held": 1, "skipped": 0, "dropped": 3}
    ]


@pytest.mark.feature("swarm-tick-cost-report")
def test_steps_dropped_at_an_expired_lane_read_skipped() -> None:
    store = Store()
    steps = [
        WriteStep("size pass", "late lane", lambda tick, get: [], writes=frozenset({Domain("tasks")})),
    ]
    with recording(store), pool.running(critical_s=0.0):
        records = engine.run_group("late lane", Context(View(), Deadline.after(60.0)), steps)
    assert records[0].skipped and records[0].abandoned
    assert [(row["step"], row["skipped"], row["abandoned"], row["dropped"]) for row in store.records()] == [
        ("size pass", True, True, 0)
    ]


@pytest.mark.feature("swarm-tick-cost-report")
def test_records_carry_lane_and_start_offset_and_report_the_critical_lane_end() -> None:
    passes = [
        {"pass_id": "a", "start_ms": 0, "end_ms": 50000, "wall_s": 50.0},
        {"pass_id": "b", "start_ms": 60000, "end_ms": 90000, "wall_s": 30.0},
    ]
    records = [
        row("a", "spawn", "tick", 2.0, 10.0),
        row("a", "sweep", "slow", 5.0, 40.0),
        row("b", "spawn", "tick", 1.0, 4.0),
        row("orphan", "spawn", "tick", 90.0, 1.0),
    ]
    assert tick_report.summarize(passes, [], records)["critical_end_p95_s"] == 12.0


def test_record_row_without_a_recording_pass_writes_nothing() -> None:
    store = Store()
    with timing.tick("sw", store):
        engine.run_group("planted", Context(View(), Deadline.after(60.0)), [ok_step()])
    assert store.records() == []


def test_finished_step_row_names_its_lane_and_offset() -> None:
    store = Store()
    with recording(store):
        engine.run_group("planted", Context(View(), Deadline.after(60.0)), [ok_step()])
    (found,) = store.records()
    assert found["lane"] == "tick" and found["outcome"] == engine.OK and found["conflicts"] == 0
    assert found["start_s"] >= 0 and not found["skipped"] and found["dropped"] == 0


def ok_step() -> WriteStep:
    return WriteStep("size pass", "planted", lambda tick, get: [])


def row(pass_id: str, name: str, lane: str, start: float, wall: float) -> dict[str, Any]:
    return {
        "pass_id": pass_id,
        "step": name,
        "lane": lane,
        "outcome": "ok",
        "wall_s": wall,
        "budget_s": 60.0,
        "start_s": start,
        "abandoned": False,
        "held": False,
        "skipped": False,
        "dropped": 0,
        "conflicts": 0,
    }
