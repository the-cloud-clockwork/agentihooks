"""Command line ledger requests timed through request metrics as server requests are: one timing line per request
with its wall, CPU, waits and each substep's wall and CPU, written only to the file the timing variable names."""

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
from scripts.swarm_ledger.repository import sqlite
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository
from scripts.tick.adapters import database_ledger
from tests.swarm_ledger.test_host_entries import kept
from tests.swarm_ledger.test_request_metrics_steps import CLAIM, CONTENT, STEP_KEYS, Clock, write_handler
from tests.swarm_ledger.test_sqlite import chat

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))
core: Any = importlib.import_module("ledger_core")
new_ledger: Any = importlib.import_module("new_ledger")

SLUG = "timing"
DOWN = "http://127.0.0.1:9"
OPERATIONS = "/api/v1/ledgers/timing/operations"
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
RECORD_KEYS = sorted(
    (*STEP_KEYS, "wall_seconds", "cpu_seconds", "lock_wait_seconds", "client_wait_seconds", "postgres_wait_seconds")
)


def quiet(slug: str, state: dict[str, Any]) -> None:
    return None


def unfenced(slug: str, epoch: int) -> None:
    return None


def timed_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch, memory_path: Path) -> Iterator[Path]:
    """A ledger on disk served in process with its ledger server down and its write hooks quiet."""
    monkeypatch.setattr(core, "LEDGER_DIR", memory_path)
    monkeypatch.setenv("LEDGER_DIR", str(memory_path))
    content = {"title": "Timing", "overview": "o", "phases": [{"title": "One", "description": "d"}]}
    assert new_ledger.create(SLUG, content)
    monkeypatch.setattr(api_service, "relay_to_inbox", quiet)
    monkeypatch.setattr(api_service, "doctor_phrase", quiet)
    monkeypatch.setattr(api_service, "deliver_alerts", quiet)
    monkeypatch.setattr(ledger_server.authority, "fence", unfenced)
    with (
        patch("hooks._redis.get_redis", return_value=None),
        patch("scripts.gates.talk.Budget._marks", return_value=None),
    ):
        yield memory_path


@pytest.fixture
def timing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "timing" / "write.jsonl"
    target.parent.mkdir()
    monkeypatch.setenv(metrics.TIMING_FILE, str(target))
    return target


def comment(text: str) -> dict[str, Any]:
    return {"op": "add", "id": f"add-{abs(hash(text))}", "by": "swarm", "thread": "phases/p1/comments", "text": text}


@pytest.mark.feature("ledger-command-line-write-timing")
def test_a_command_line_comment_write_keeps_its_reply_and_leaves_no_timing_set(stored: Path) -> None:
    entry = comment("Write landed")
    reply: Any = database_ledger.client(DOWN, 30.0).mutate(SLUG, [entry])
    assert reply["rejected"] == []
    assert reply["applied"] == [entry["id"]]
    assert metrics.CURRENT.get() is None
    assert [item["text"] for item in core.sync(SLUG)[0]["phases"][0]["comments"]] == ["Write landed"]


@pytest.mark.feature("ledger-command-line-write-timing")
def test_a_server_write_record_keeps_only_its_four_substep_columns(tmp_path: Path) -> None:
    repo = SQLiteLedgerRepository(tmp_path / "ledgers" / sqlite.DATABASE)
    repo.create("ledger", CONTENT)

    def handle_one_request(handler: SimpleNamespace) -> None:
        repo.apply_ops("ledger", ops=[CLAIM])

    served: Any = metrics.measured(handle_one_request)
    served(write_handler())
    found: Any = metrics.records(1.0, None, 10)
    [record] = found["routes"][0]["records"]
    assert sorted(key for key in record if key.endswith("_seconds")) == RECORD_KEYS


@pytest.mark.feature("ledger-command-line-write-timing")
def test_a_command_line_request_appends_one_timing_line_to_the_named_file(
    timing_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    monkeypatch.setattr(metrics, "time", clock)
    with metrics.command("POST", OPERATIONS):
        clock.wall += 1.0
        clock.cpu += 0.5
        with metrics.step("guard_read"):
            clock.wall += 2.0
            clock.cpu += 1.5
    assert timed_lines(timing_file) == [
        {
            "method": "POST",
            "route": "/api/v1/ledgers/:slug/operations",
            "detail": "",
            "wall": 3.0,
            "cpu": 2.0,
            "lock_wait": 0.0,
            "client_wait": 0.0,
            "postgres_wait": 0.0,
            "steps": {"guard_read": {"wall": 2.0, "cpu": 1.5}},
        }
    ]
    assert metrics.CURRENT.get() is None
    assert list(metrics.SAMPLES) == []


@pytest.mark.feature("ledger-command-line-write-timing")
def test_each_command_line_request_appends_its_own_line(timing_file: Path) -> None:
    with metrics.command("GET", "/api/v1/ledgers/timing/phases/p1"):
        pass
    with metrics.command("POST", OPERATIONS), metrics.step("publish"):
        pass
    assert [(line["method"], sorted(line["steps"])) for line in timed_lines(timing_file)] == [
        ("GET", []),
        ("POST", ["publish"]),
    ]


@pytest.mark.feature("ledger-command-line-write-timing")
def test_a_command_line_request_writes_nothing_without_the_timing_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(metrics.TIMING_FILE, raising=False)
    monkeypatch.chdir(tmp_path)
    with metrics.command("POST", OPERATIONS), metrics.step("guard_read"):
        pass
    assert list(tmp_path.iterdir()) == []
    assert list(metrics.SAMPLES) == []
    assert metrics.CURRENT.get() is None


@pytest.mark.feature("ledger-command-line-write-timing")
def test_a_command_line_request_inside_a_server_request_joins_it_and_writes_no_line(
    timing_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    monkeypatch.setattr(metrics, "time", clock)

    def handle_one_request(handler: SimpleNamespace) -> None:
        with metrics.command("POST", OPERATIONS), metrics.step("hierarchy_sync"):
            clock.wall += 1.0

    served: Any = metrics.measured(handle_one_request)
    served(write_handler())
    found: Any = metrics.records(1.0, None, 10)
    [record] = found["routes"][0]["records"]
    assert record["hierarchy_sync_seconds"] == 1.0
    assert not timing_file.exists()


@pytest.mark.feature("ledger-command-line-write-timing")
def test_a_command_line_comment_write_records_its_requests_and_write_substeps(stored: Path, timing_file: Path) -> None:
    reply: Any = database_ledger.client(DOWN, 30.0).mutate(SLUG, [comment("Timed write")])
    assert reply["rejected"] == []
    lines = timed_lines(timing_file)
    assert [line["method"] for line in lines] == ["GET", "POST"]
    assert lines[-1]["route"] == "/api/v1/ledgers/:slug/operations"
    assert WRITE_STEPS <= set(lines[-1]["steps"])
    assert all(set(step) == {"wall", "cpu"} for line in lines for step in line["steps"].values())
    assert metrics.CURRENT.get() is None


@pytest.mark.feature("ledger-command-line-write-timing")
def test_a_fresh_repository_records_the_whole_load_of_its_ledger(tmp_path: Path, timing_file: Path) -> None:
    path = tmp_path / "ledgers" / sqlite.DATABASE
    SQLiteLedgerRepository(path).create("ledger", CONTENT)
    with metrics.command("GET", "/api/v1/ledgers/ledger/export"):
        SQLiteLedgerRepository(path).current("ledger")
    [line] = timed_lines(timing_file)
    assert {"copy_refresh", "copy_load", "copy_document"} <= set(line["steps"])
    assert "copy_adopt" not in line["steps"]


@pytest.mark.feature("ledger-command-line-write-timing")
def test_a_fresh_repository_over_a_host_copy_records_its_adoption(tmp_path: Path, timing_file: Path) -> None:
    first = kept(tmp_path)
    first.create("ledger", CONTENT)
    first.current("ledger")
    first.apply_ops("ledger", ops=[chat(1)])
    with metrics.command("GET", "/api/v1/ledgers/ledger/export"):
        kept(tmp_path).current("ledger")
    [line] = timed_lines(timing_file)
    assert "copy_adopt" in line["steps"]
    assert "copy_load" not in line["steps"]


@pytest.mark.feature("ledger-command-line-write-timing")
def test_a_write_under_the_ledger_lock_records_its_apply(tmp_path: Path, timing_file: Path) -> None:
    repo = SQLiteLedgerRepository(tmp_path / "ledgers" / sqlite.DATABASE)
    repo.create("ledger", CONTENT)
    with metrics.command("POST", "/api/v1/ledgers/ledger/operations"):
        repo.apply_ops("ledger", ops=[CLAIM])
    [line] = timed_lines(timing_file)
    assert {"apply_locked", *(key.removesuffix("_seconds") for key in STEP_KEYS)} <= set(line["steps"])
