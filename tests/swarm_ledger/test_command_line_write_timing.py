"""Command line ledger requests timed through request metrics: each in process request appends one timing line, with
every write substep's wall and CPU, to the file the timing variable names."""

import importlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from scripts.swarm_ledger import api_service, ledger_server
from scripts.swarm_ledger import request_metrics as metrics
from scripts.swarm_ledger.repository import host_entries, sqlite

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))
cli_ledger: Any = importlib.import_module("ledger")
core: Any = importlib.import_module("ledger_core")
new_ledger: Any = importlib.import_module("new_ledger")

pytestmark = pytest.mark.unit

SLUG = "write-timing"
DOWN = "http://127.0.0.1:9"
OPERATIONS = "/api/v1/ledgers/:slug/operations"
CONTENT = {
    "title": "Write timing",
    "overview": "o",
    "sources": [],
    "phases": [{"title": "One", "description": "d"}],
    "tasks": [{"title": "first", "phase": "p1", "lane": "eng"}],
}
CLAIM = {"op": "task_update", "id": "u1", "by": "eng", "item": "tasks/t1", "fields": {"state": "claimed"}}
WRITE_STEPS = {
    "guard_read",
    "write_gate",
    "apply_locked",
    "publish",
    "hierarchy_sync",
    "event_append",
    "outbox_write",
    "refresh_record",
}
RECORD_KEYS = {
    "ended",
    "wall_seconds",
    "cpu_seconds",
    "lock_wait_seconds",
    "client_wait_seconds",
    "postgres_wait_seconds",
    "hierarchy_sync_seconds",
    "outbox_write_seconds",
    "refresh_record_seconds",
    "event_append_seconds",
}


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
    monkeypatch.setattr(metrics, "time", clock)
    return clock


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch, memory_path: Path) -> Iterator[Path]:
    """A ledger on disk behind no ledger server, its publish side effects stubbed; yields the timing file path."""
    monkeypatch.setattr(core, "LEDGER_DIR", memory_path)
    monkeypatch.setenv("LEDGER_DIR", str(memory_path))
    assert new_ledger.create(SLUG, CONTENT)
    monkeypatch.setattr(cli_ledger, "BASE", DOWN)
    monkeypatch.setattr(api_service, "relay_to_inbox", lambda slug, state: None)
    monkeypatch.setattr(api_service, "doctor_phrase", lambda slug, state: None)
    monkeypatch.setattr(api_service, "deliver_alerts", lambda slug, state: None)
    monkeypatch.setattr(ledger_server.authority, "fence", lambda slug, epoch: None)
    with (
        patch("hooks._redis.get_redis", return_value=None),
        patch("scripts.gates.talk.Budget._marks", return_value=None),
    ):
        yield memory_path / "timing.jsonl"


def comment(text: str) -> dict[str, Any]:
    return {"op": "add", "id": "c-timing0001", "by": "swarm", "thread": "phases/p1/comments", "text": text}


def timed(target: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in target.read_text().splitlines()]


def write_handler() -> SimpleNamespace:
    return SimpleNamespace(command="POST", path="/api/v1/ledgers/timing/operations", raw_requestline=b"POST")


def kept(tmp_path: Path) -> sqlite.SQLiteLedgerRepository:
    repository = sqlite.SQLiteLedgerRepository(tmp_path / "ledgers" / sqlite.DATABASE)
    repository.copies = host_entries.Copies(tmp_path / "home" / "entries", str(repository.path))
    return repository


@pytest.mark.feature("ledger-command-line-write-timing")
def test_a_command_line_comment_write_keeps_its_reply_and_leaves_no_timing_context(stored: Path) -> None:
    reply = cli_ledger.request(SLUG, [comment("Kept reply")])
    assert reply["applied"] == ["c-timing0001"]
    assert reply["rejected"] == []
    assert metrics.CURRENT.get() is None
    assert [entry["text"] for entry in core.sync(SLUG)[0]["phases"][0]["comments"]] == ["Kept reply"]


@pytest.mark.feature("ledger-write-substep-timing")
def test_a_server_write_row_keeps_only_its_four_substep_columns(tmp_path: Path) -> None:
    repo = sqlite.SQLiteLedgerRepository(tmp_path / "ledgers" / sqlite.DATABASE)
    repo.create("ledger", CONTENT)

    def handle_one_request(handler: SimpleNamespace) -> None:
        sqlite.SQLiteLedgerRepository(repo.path).apply_ops("ledger", ops=[CLAIM])

    metrics.measured(handle_one_request)(write_handler())
    [record] = metrics.records(1.0, None, 10)["routes"][0]["records"]
    assert set(record) == RECORD_KEYS


@pytest.mark.feature("ledger-command-line-write-timing")
def test_a_command_line_request_appends_one_timing_line_to_the_named_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    target = tmp_path / "timing.jsonl"
    monkeypatch.setenv(metrics.TIMING_FILE, str(target))
    with metrics.command("POST", "/api/v1/ledgers/ledger/operations"):
        clock.wall, clock.cpu = 1.0, 0.25
        with metrics.step("guard_read"):
            clock.wall, clock.cpu = 3.0, 0.75
        metrics.detailed("add")
        clock.wall, clock.cpu = 4.0, 1.0
    [line] = timed(target)
    assert (line["method"], line["route"], line["detail"]) == ("POST", OPERATIONS, "add")
    assert (line["wall"], line["cpu"]) == (4.0, 1.0)
    assert (line["lock_wait"], line["client_wait"], line["postgres_wait"]) == (0.0, 0.0, 0.0)
    assert line["steps"] == {"guard_read": {"wall": 2.0, "cpu": 0.5}}
    assert metrics.CURRENT.get() is None


@pytest.mark.feature("ledger-command-line-write-timing")
def test_a_command_line_request_writes_nothing_without_the_timing_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(metrics.TIMING_FILE, raising=False)
    monkeypatch.chdir(tmp_path)
    with metrics.command("POST", "/api/v1/ledgers/ledger/operations"), metrics.step("guard_read"):
        pass
    assert list(tmp_path.iterdir()) == []
    assert list(metrics.SAMPLES) == []
    assert metrics.CURRENT.get() is None


@pytest.mark.feature("ledger-command-line-write-timing")
def test_a_command_line_request_inside_a_server_request_adds_its_steps_to_the_server_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    target = tmp_path / "timing.jsonl"
    monkeypatch.setenv(metrics.TIMING_FILE, str(target))

    def handle_one_request(handler: SimpleNamespace) -> None:
        with metrics.command("POST", "/api/v1/ledgers/ledger/operations"), metrics.step("hierarchy_sync"):
            clock.wall += 2.0

    metrics.measured(handle_one_request)(write_handler())
    [record] = metrics.records(1.0, None, 10)["routes"][0]["records"]
    assert record["hierarchy_sync_seconds"] == 2.0
    assert not target.exists()


@pytest.mark.feature("ledger-command-line-write-timing")
def test_a_command_line_comment_write_times_every_write_substep(stored: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(metrics.TIMING_FILE, str(stored))
    cli_ledger.request(SLUG, [comment("Timed write")])
    [write] = [line for line in timed(stored) if line["method"] == "POST"]
    assert write["route"] == OPERATIONS
    assert WRITE_STEPS <= set(write["steps"])
    assert all(set(figures) == {"wall", "cpu"} for figures in write["steps"].values())
    assert write["wall"] >= write["steps"]["apply_locked"]["wall"]


@pytest.mark.feature("ledger-command-line-write-timing")
def test_a_fresh_repository_times_its_whole_load_and_document_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ledgers" / sqlite.DATABASE
    sqlite.SQLiteLedgerRepository(path).create("ledger", CONTENT)
    target = tmp_path / "timing.jsonl"
    monkeypatch.setenv(metrics.TIMING_FILE, str(target))
    with metrics.command("GET", "/api/v1/ledgers/ledger"):
        sqlite.SQLiteLedgerRepository(path).current("ledger")
    [line] = timed(target)
    assert {"copy_load", "copy_document"} <= set(line["steps"])
    assert "copy_adopt" not in line["steps"]


@pytest.mark.feature("ledger-command-line-write-timing")
def test_a_fresh_repository_over_a_host_copy_times_its_copy_adopt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = kept(tmp_path)
    first.create("ledger", CONTENT)
    first.current("ledger")
    target = tmp_path / "timing.jsonl"
    monkeypatch.setenv(metrics.TIMING_FILE, str(target))
    with metrics.command("GET", "/api/v1/ledgers/ledger"):
        kept(tmp_path).current("ledger")
    [line] = timed(target)
    assert {"copy_adopt", "copy_refresh"} <= set(line["steps"])
    assert "copy_load" not in line["steps"]
