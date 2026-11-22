"""The time writes command: N agent comment writes one after another, each a fresh process with its own timing file,
reported per write with the median and max of every figure."""

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.swarm_ledger import TOOLS, request_metrics, write_timing

AGENT = ["--slug", "scratch", "--as", "eng@a"]
OPERATIONS = "/api/v1/ledgers/:slug/operations"


def line(wall: float, steps: dict[str, float], method: str = "POST") -> dict[str, Any]:
    return {
        "method": method,
        "route": OPERATIONS if method == "POST" else "/api/v1/ledgers/:slug/phases/:item",
        "detail": "",
        "wall": wall,
        "cpu": wall / 2,
        "lock_wait": 0.25,
        "client_wait": 0.0,
        "postgres_wait": 0.5,
        "steps": {name: {"wall": seconds, "cpu": seconds / 2} for name, seconds in steps.items()},
    }


class Children:
    """Stand in write processes: each records its command and timing file and leaves its given lines there."""

    def __init__(self, runs: list[tuple[int, list[dict[str, Any]]]]) -> None:
        self.runs = runs
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(self, argv: list[str], timing: Path) -> write_timing.Child:
        code, lines = self.runs[len(self.calls)]
        self.calls.append((argv, timing))
        timing.write_text("".join(json.dumps(item) + "\n" for item in lines))
        return write_timing.Child(code, 4.0 * len(self.calls), 2.0 * len(self.calls))


def reported(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    found: dict[str, Any] = json.loads(capsys.readouterr().out)
    return found


@pytest.mark.feature("ledger-write-timing-baseline-command")
def test_each_write_is_a_fresh_agent_comment_process_with_its_own_timing_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    children = Children([(0, [line(1.0, {})]), (0, [line(1.0, {})])])
    monkeypatch.setattr(write_timing, "spawned", children)
    write_timing.main([*AGENT, "--count", "2", "t1"])
    agent = [sys.executable, "-m", "scripts.install", "api", *AGENT, "comment", "tasks/t1", write_timing.TEXT]
    assert [argv for argv, _ in children.calls] == [agent, agent]
    assert len({timing for _, timing in children.calls}) == 2
    assert reported(capsys)["count"] == 2


@pytest.mark.feature("ledger-write-timing-baseline-command")
def test_the_report_carries_each_write_and_the_median_and_max_of_every_figure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    children = Children(
        [
            (0, [line(1.0, {"copy_load": 3.0}, "GET"), line(2.0, {"guard_read": 1.0, "apply_locked": 0.5})]),
            (0, [line(1.0, {"copy_adopt": 0.5}, "GET"), line(4.0, {"guard_read": 3.0, "apply_locked": 1.5})]),
            (0, [line(1.0, {}, "GET"), line(6.0, {"guard_read": 2.0, "apply_locked": 1.0})]),
        ]
    )
    monkeypatch.setattr(write_timing, "spawned", children)
    write_timing.main([*AGENT, "--count", "3", "--text", "Timing probe", "t9"])
    report = reported(capsys)
    assert (report["slug"], report["task"], report["count"]) == ("scratch", "t9", 3)
    assert children.calls[0][0][-2:] == ["tasks/t9", "Timing probe"]
    assert len(report["load_before"]) == 3
    assert len(report["load_after"]) == 3
    assert report["writes"][0] == {
        "process_wall": 4.0,
        "process_cpu": 2.0,
        "requests": 2,
        "request_wall": 3.0,
        "request_cpu": 1.5,
        "write_wall": 2.0,
        "write_cpu": 1.0,
        "lock_wait": 0.5,
        "postgres_wait": 1.0,
        "copy_load_wall": 3.0,
        "copy_load_cpu": 1.5,
        "guard_read_wall": 1.0,
        "guard_read_cpu": 0.5,
        "apply_locked_wall": 0.5,
        "apply_locked_cpu": 0.25,
    }
    summary = report["summary"]
    assert summary["process_wall"] == {"median": 8.0, "max": 12.0}
    assert summary["write_wall"] == {"median": 4.0, "max": 6.0}
    assert summary["guard_read_wall"] == {"median": 2.0, "max": 3.0}
    assert summary["copy_adopt_wall"] == {"median": 0.0, "max": 0.5}
    assert summary["copy_load_cpu"] == {"median": 0.0, "max": 1.5}


@pytest.mark.feature("ledger-write-timing-baseline-command")
def test_a_failing_write_stops_the_run_with_its_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    children = Children([(0, [line(1.0, {})]), (3, []), (0, [line(1.0, {})])])
    monkeypatch.setattr(write_timing, "spawned", children)
    with pytest.raises(SystemExit) as stopped:
        write_timing.main([*AGENT, "--count", "3", "t1"])
    assert stopped.value.code == 3
    assert len(children.calls) == 2
    assert "write 2 of 3 exited 3" in capsys.readouterr().err


@pytest.mark.feature("ledger-write-timing-baseline-command")
def test_a_write_process_gets_its_timing_file_and_keeps_its_exit_code(tmp_path: Path) -> None:
    timing = tmp_path / "write.jsonl"
    script = (
        f"import os, pathlib; pathlib.Path(os.environ[{request_metrics.TIMING_FILE!r}]).write_text('seen'); "
        "raise SystemExit(5)"
    )
    child = write_timing.spawned([sys.executable, "-c", script], timing)
    assert child.code == 5
    assert timing.read_text() == "seen"
    assert child.wall > 0.0
    assert child.cpu >= 0.0


@pytest.mark.feature("ledger-write-timing-baseline-command")
def test_the_time_writes_command_is_a_ledger_tool() -> None:
    assert TOOLS["time-writes"] == ("scripts.swarm_ledger.write_timing", [])
