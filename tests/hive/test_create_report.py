"""Hive create writes each finished step with its UTC time the moment the step ends."""

import io
import os
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path
from unittest import mock

import pytest

from scripts.hive import create

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
NOW = 1_800_000_000
STAMPED = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ hive create swarm-hive-009: step (\d): stub (\d) done\n")
CHILD = """
import sys
from scripts.hive import create

def stub(n):
    def step(tools, options):
        sys.stdin.readline()
        return f"stub {n} done"
    return step

create.STEPS = tuple((f"step {n}", stub(n)) for n in range(3))
create._placing = lambda tools: None
create._signer_path = lambda environ: ""
create._start = lambda tools, options: {"name": "swarm-hive-009"}
create.create(create.Tools(None, None, {}, "root", "redis://"), create.Options(dry_run=True))
"""


class Flushes(io.StringIO):
    """A text stream that records what it held at each flush."""

    def __init__(self) -> None:
        super().__init__()
        self.flushed: list[str] = []

    def flush(self) -> None:
        """Record the text written so far."""
        self.flushed.append(self.getvalue())


def _lines(stream: io.BufferedReader, into: "queue.Queue[str]") -> None:
    for raw in iter(stream.readline, b""):
        into.put(raw.decode())


def test_each_step_line_reaches_a_pipe_before_the_next_step_is_released() -> None:
    env = {key: value for key, value in os.environ.items() if key != "PYTHONUNBUFFERED"}
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD], cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )
    assert child.stdin is not None and child.stdout is not None
    arrived: queue.Queue[str] = queue.Queue()
    threading.Thread(target=_lines, args=(child.stdout, arrived), daemon=True).start()
    try:
        for step in range(3):
            child.stdin.write(b"go\n")
            child.stdin.flush()
            line = arrived.get(timeout=30)
            match = STAMPED.fullmatch(line)
            assert match is not None, line
            assert match.groups() == (str(step), str(step))
        child.stdin.close()
        assert child.wait(timeout=30) == 0
    finally:
        child.kill()


def test_the_report_stamps_the_line_with_its_utc_time_and_flushes_it() -> None:
    held = Flushes()
    with mock.patch.object(sys, "stdout", held):
        create.report("hive create swarm-hive-009: invite: invited", clock=lambda: NOW)

    assert held.flushed == ["2027-01-15T08:00:00Z hive create swarm-hive-009: invite: invited\n"]


def test_the_report_reads_the_clock_when_it_writes(capsys: pytest.CaptureFixture[str]) -> None:
    times = iter([NOW, NOW + 61])
    create.report("first", clock=lambda: next(times))
    create.report("second", clock=lambda: next(times))

    assert capsys.readouterr().out == "2027-01-15T08:00:00Z first\n2027-01-15T08:01:01Z second\n"


def test_tools_report_by_default() -> None:
    assert create.Tools(mock.Mock(), mock.Mock(), {}, "root", "redis://").out is create.report
