"""The time writes command: N agent comment writes, each in a fresh command line process with its own timing file,
reported per write and as the median and max of every figure."""

import contextlib
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.swarm_ledger import request_metrics, run, write_timing

pytestmark = pytest.mark.unit

ARGV = ["--slug", "scratch", "--as", "engineer@timing", "--count", "3", "t1"]
COMMAND = [
    sys.executable,
    "-m",
    "scripts.install",
    "api",
    "--slug",
    "scratch",
    "--as",
    "engineer@timing",
    "comment",
    "tasks/t1",
    write_timing.TEXT,
]


class Children:
    def __init__(self, walls: list[float], steps: list[dict[str, dict[str, float]]], codes: list[int]) -> None:
        self.walls, self.steps, self.codes = walls, steps, codes
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, argv: list[str], environ: dict[str, str]) -> write_timing.Finished:
        index = len(self.calls)
        self.calls.append((argv, environ))
        line = {
            "method": "POST",
            "route": "/api/v1/ledgers/:slug/operations",
            "detail": "add",
            "wall": self.walls[index] / 2,
            "cpu": self.walls[index] / 4,
            "lock_wait": 0.5,
            "client_wait": 0.0,
            "postgres_wait": 0.0,
            "steps": self.steps[index],
        }
        read = {**line, "method": "GET", "steps": {"copy_load": {"wall": 9.0, "cpu": 9.0}}}
        Path(environ[request_metrics.TIMING_FILE]).write_text(json.dumps(read) + "\n" + json.dumps(line) + "\n")
        return write_timing.Finished(self.codes[index], self.walls[index], self.walls[index] / 3, "refused here")


def reported(capsys: pytest.CaptureFixture[str], children: Children) -> tuple[int, dict[str, Any]]:
    code = write_timing.main(ARGV, run=children)
    out = capsys.readouterr().out
    return code, json.loads(out) if out else {}


@pytest.mark.feature("ledger-write-timing-baseline-command")
def test_each_write_runs_the_agent_comment_command_with_its_own_timing_file(
    capsys: pytest.CaptureFixture[str],
) -> None:
    children = Children([1.0, 2.0, 3.0], [{}, {}, {}], [0, 0, 0])
    code, _ = reported(capsys, children)
    assert code == 0
    assert [argv for argv, _ in children.calls] == [COMMAND] * 3
    files = {environ[request_metrics.TIMING_FILE] for _, environ in children.calls}
    assert len(files) == 3
    assert all(environ["PATH"] == os.environ["PATH"] for _, environ in children.calls)


@pytest.mark.feature("ledger-write-timing-baseline-command")
def test_the_report_carries_each_write_and_the_median_and_max_of_every_figure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    guard = [{"guard_read": {"wall": wall, "cpu": wall / 2}} for wall in (1.0, 3.0)]
    children = Children([3.0, 9.0, 6.0], [*guard, {}], [0, 0, 0])
    _, report = reported(capsys, children)
    assert (report["slug"], report["task"], report["count"]) == ("scratch", "t1", 3)
    assert [write["process_wall"] for write in report["writes"]] == [3.0, 9.0, 6.0]
    assert report["writes"][0]["write_wall"] == 1.5
    assert report["writes"][0]["requests"] == 2
    assert report["writes"][0]["steps"] == {"guard_read": {"wall": 1.0, "cpu": 0.5}}
    summary = report["summary"]
    assert summary["process_wall"] == {"median": 6.0, "max": 9.0}
    assert summary["process_cpu"] == {"median": 2.0, "max": 3.0}
    assert summary["write_wall"] == {"median": 3.0, "max": 4.5}
    assert summary["write_cpu"] == {"median": 1.5, "max": 2.25}
    assert summary["lock_wait"] == {"median": 0.5, "max": 0.5}
    assert summary["guard_read_wall"] == {"median": 1.0, "max": 3.0}
    assert summary["guard_read_cpu"] == {"median": 0.5, "max": 1.5}
    assert "copy_load_wall" not in summary
    assert len(report["load"]["before"]) == len(report["load"]["after"]) == 3


@pytest.mark.feature("ledger-write-timing-baseline-command")
def test_a_failing_write_stops_the_run_with_its_exit_code(capsys: pytest.CaptureFixture[str]) -> None:
    children = Children([1.0, 2.0, 3.0], [{}, {}, {}], [0, 3, 0])
    code = write_timing.main(ARGV, run=children)
    captured = capsys.readouterr()
    assert code == 3
    assert len(children.calls) == 2
    assert captured.out == ""
    assert "write 2 exited 3: refused here" in captured.err


@pytest.mark.feature("ledger-write-timing-baseline-command")
def test_a_spawned_child_reports_its_exit_code_wall_and_cpu() -> None:
    busy = write_timing.spawned([sys.executable, "-c", "sum(range(3000000))"], dict(os.environ))
    failed = write_timing.spawned([sys.executable, "-c", "import sys; sys.exit(4)"], dict(os.environ))
    assert busy.code == 0
    assert busy.wall > 0.0
    assert busy.cpu > 0.0
    assert failed.code == 4


@pytest.mark.feature("ledger-write-timing-baseline-command")
def test_the_ledger_tools_run_time_writes() -> None:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), pytest.raises(SystemExit) as exited:
        run(["time-writes", "--help"])
    assert exited.value.code == 0
    assert "agentihooks ledger time-writes" in out.getvalue()
    assert "agentihooks api" in out.getvalue()
