import json
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import request_metrics as metrics
from scripts.swarm_ledger.repository import hierarchy, outbox, refresh, sqlite
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

OPERATIONS = "/api/v1/ledgers/:slug/operations"
CONTENT = {
    "title": "Store",
    "overview": "o",
    "sources": [],
    "phases": [{"title": "One", "description": "d"}],
    "tasks": [{"title": "first", "phase": "p1", "lane": "eng"}],
}
CLAIM = {"op": "task_update", "id": "u1", "by": "eng", "item": "tasks/t1", "fields": {"state": "claimed"}}
STEP_KEYS = ("hierarchy_sync_seconds", "outbox_write_seconds", "refresh_record_seconds", "event_append_seconds")


class Clock:
    def __init__(self) -> None:
        self.wall = 0.0
        self.cpu = 0.0

    def perf_counter(self) -> float:
        return self.wall

    def thread_time(self) -> float:
        return self.cpu

    def time(self) -> float:
        return 1791856800.0 + self.wall


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(metrics, "SAMPLES", deque(maxlen=64))
    monkeypatch.setattr(metrics, "STARTED_AT", clock.time() - metrics.RETENTION_SECONDS)
    monkeypatch.setattr(metrics, "EVICTED_THROUGH", float("-inf"))
    monkeypatch.setattr(metrics, "JOURNAL", None)
    monkeypatch.setattr(metrics, "HELD_SINCE", None)
    monkeypatch.setattr(metrics, "LOST", [])
    monkeypatch.setattr(metrics, "PENDING", [])
    monkeypatch.setattr(metrics, "APPENDED", 0)
    monkeypatch.setattr(metrics, "STALE", False)
    monkeypatch.setattr(metrics, "time", clock)
    return clock


def write_handler() -> SimpleNamespace:
    return SimpleNamespace(command="POST", path="/api/v1/ledgers/timing/operations", raw_requestline=b"POST")


def slowed(monkeypatch: pytest.MonkeyPatch, clock: Clock, target: object, name: str, seconds: float) -> None:
    original = getattr(target, name)

    def run(*args: object, **kwargs: object) -> object:
        clock.wall += seconds
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, run)


@pytest.mark.feature("ledger-write-substep-timing")
def test_a_ledger_write_metrics_row_carries_each_substep_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    repo = SQLiteLedgerRepository(tmp_path / "ledgers" / sqlite.DATABASE)
    repo.create("ledger", CONTENT)
    slowed(monkeypatch, clock, hierarchy, "sync", 1.0)
    slowed(monkeypatch, clock, outbox, "write", 2.0)
    slowed(monkeypatch, clock, refresh, "record", 4.0)
    slowed(monkeypatch, clock, sqlite, "append_events", 8.0)

    def handle_one_request(handler: SimpleNamespace) -> None:
        repo.apply_ops("ledger", ops=[CLAIM])

    metrics.measured(handle_one_request)(write_handler())
    [record] = metrics.records(1.0, None, 10)["routes"][0]["records"]
    assert [record[key] for key in STEP_KEYS] == [1.0, 2.0, 4.0, 8.0]
    assert record["wall_seconds"] == 15.0


@pytest.mark.feature("ledger-write-substep-timing")
def test_substeps_outside_a_measured_request_are_not_counted(clock: Clock) -> None:
    with metrics.step("hierarchy_sync"):
        clock.wall += 1.0
    assert list(metrics.SAMPLES) == []

    def handle_one_request(handler: SimpleNamespace) -> None:
        clock.wall += 2.0

    metrics.measured(handle_one_request)(write_handler())
    [record] = metrics.records(1.0, None, 10)["routes"][0]["records"]
    assert [record[key] for key in STEP_KEYS] == [0.0, 0.0, 0.0, 0.0]
    assert record["wall_seconds"] == 2.0


@pytest.mark.feature("ledger-write-substep-timing")
def test_a_substep_run_twice_in_one_request_adds_both_times(clock: Clock) -> None:
    def handle_one_request(handler: SimpleNamespace) -> None:
        with metrics.step("outbox_write"):
            clock.wall += 1.0
        with metrics.step("outbox_write"):
            clock.wall += 2.0

    metrics.measured(handle_one_request)(write_handler())
    [record] = metrics.records(1.0, None, 10)["routes"][0]["records"]
    assert [record[key] for key in STEP_KEYS] == [0.0, 3.0, 0.0, 0.0]


@pytest.mark.feature("ledger-write-substep-timing")
def test_the_summary_totals_each_write_substep_and_keeps_other_wall(clock: Clock) -> None:
    steps = {"hierarchy_sync": 1.0, "outbox_write": 2.0, "refresh_record": 3.0, "event_append": 0.5}
    metrics.record("POST", OPERATIONS, 10.0, 1.0, 2.0, metrics.Timing(steps=steps))
    metrics.record("POST", OPERATIONS, 4.0, 1.0, 0.0, metrics.Timing(steps={"outbox_write": 1.5}))
    metrics.record("GET", OPERATIONS, 50.0, 1.0, 0.0, metrics.Timing(steps=steps))
    writes = metrics.report(1.0)["writes"]
    assert [writes[key] for key in STEP_KEYS] == [1.0, 3.5, 3.0, 0.5]
    assert writes["other_wall_seconds"] == 10.0


@pytest.mark.feature("ledger-write-substep-timing")
def test_a_journal_sample_carries_its_substeps_across_a_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    path = tmp_path / "journal" / "metrics.jsonl"
    path.parent.mkdir()
    metrics.restore(path)
    steps = {"hierarchy_sync": 1.0, "outbox_write": 2.0, "refresh_record": 3.0, "event_append": 4.0}
    metrics.record("POST", OPERATIONS, 12.0, 1.0, 0.0, metrics.Timing(steps=steps))
    metrics.persist(stopped=True)
    [sample] = [row for row in map(json.loads, path.read_text().splitlines()) if row[0] == "s"]
    assert sample[-4:] == [1.0, 2.0, 3.0, 4.0]
    monkeypatch.setattr(metrics, "SAMPLES", deque(maxlen=64))
    monkeypatch.setattr(metrics, "JOURNAL", None)
    metrics.restore(path)
    [record] = metrics.records(1.0, None, 10)["routes"][0]["records"]
    assert [record[key] for key in STEP_KEYS] == [1.0, 2.0, 3.0, 4.0]


def test_journal_samples_of_every_known_width_are_read() -> None:
    assert metrics.journal_row(["s", 1.0, "POST", OPERATIONS, 1.0, 0.0, 0.0])
    assert metrics.journal_row(["s", 1.0, "POST", OPERATIONS, 1.0, 0.0, 0.0, 0.0, 0.0])
    assert metrics.journal_row(["s", 1.0, "POST", OPERATIONS, *[0.0] * 9])
    assert not metrics.journal_row(["s", 1.0, "POST", OPERATIONS, *[0.0] * 8])
