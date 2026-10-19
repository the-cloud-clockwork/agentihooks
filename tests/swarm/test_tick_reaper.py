import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.swarm import tick_reaper

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("tick-reaper")]

ROOT = Path(__file__).resolve().parents[2]
SLEEP = "import time; time.sleep(600)"
DEAF = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(600)"
PARENT = (
    "import subprocess, sys; child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)']); "
    "print(child.pid, flush=True); child.wait()"
)


def _plant(code, *argv, stdout=None):
    return subprocess.Popen([sys.executable, "-c", code, *argv], start_new_session=True, stdout=stdout, text=True)


def _gone(pid, within=15.0):
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            return True
        if state in ("Z", "X"):
            return True
        time.sleep(0.1)
    return False


@pytest.mark.parametrize(
    ("argv", "tick"),
    [
        (["/usr/bin/python3", "/home/op/.local/bin/agentihooks", "swarm", "tick"], True),
        (["/usr/bin/python3", "-m", "scripts.swarm.scheduled_tick"], True),
        (["/usr/bin/python3", "/home/op/.local/bin/agentihooks", "swarm", "waits"], False),
        (["/usr/bin/python3", "-m", "scripts.swarm.snapshot_job", "slug"], False),
        (["/usr/bin/python3", "/home/op/.local/bin/agentihooks", "swarm", "tick", "extra"], False),
        ([], False),
    ],
)
def test_a_tick_process_is_a_swarm_tick_or_its_scheduler_holder(argv, tick):
    assert tick_reaper.is_tick(argv) is tick


def test_a_planted_leftover_tick_and_its_process_group_are_reaped_and_recorded():
    left = _plant(PARENT, "swarm", "tick", stdout=subprocess.PIPE)
    try:
        child = int(left.stdout.readline())
        reaped = tick_reaper.reap(min_age_s=0)
        mine = [record for record in reaped if record.pid == left.pid]
        assert len(mine) == 1
        assert mine[0].command.endswith("swarm tick")
        assert mine[0].age_s >= 0
        assert mine[0].signal == "SIGTERM"
        assert left.wait(15) == -signal.SIGTERM
        assert _gone(child)
    finally:
        left.kill()
        left.wait()


def test_a_leftover_that_ignores_terminate_is_killed():
    left = _plant(DEAF, "swarm", "tick", stdout=subprocess.PIPE)
    try:
        assert left.stdout.readline().strip() == "ready"
        reaped = [record for record in tick_reaper.reap(min_age_s=0, grace_s=0.5) if record.pid == left.pid]
        assert [record.signal for record in reaped] == ["SIGKILL"]
        assert left.wait(15) == -signal.SIGKILL
    finally:
        left.kill()
        left.wait()


def test_a_tick_younger_than_one_pass_is_left_running():
    young = _plant(SLEEP, "swarm", "tick")
    try:
        assert young.pid not in [record.pid for record in tick_reaper.reap()]
        assert young.poll() is None
    finally:
        young.kill()
        young.wait()


def test_the_reaping_tick_spares_itself_and_its_ancestors():
    inner = "from scripts.swarm import tick_reaper; print([r.pid for r in tick_reaper.reap(min_age_s=0)])"
    outer = (
        "import subprocess, sys; "
        f"sys.exit(subprocess.run([sys.executable, '-c', {inner!r}, 'swarm', 'tick']).returncode)"
    )
    done = subprocess.run(
        [sys.executable, "-c", outer, "swarm", "tick"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[]"


def test_a_process_of_another_user_is_never_a_leftover(monkeypatch):
    left = _plant(SLEEP, "swarm", "tick")
    try:
        monkeypatch.setattr(tick_reaper.os, "getuid", lambda: os.getuid() + 1)
        assert left.pid not in [record.pid for record in tick_reaper.reap(min_age_s=0)]
        assert left.poll() is None
    finally:
        left.kill()
        left.wait()


def test_a_tick_of_another_swarm_redis_is_never_a_leftover():
    other = {**os.environ, "AGENTIHOOKS_SWARM_REDIS_URL": "redis://127.0.0.1:6390/0"}
    left = subprocess.Popen([sys.executable, "-c", SLEEP, "swarm", "tick"], start_new_session=True, env=other)
    try:
        assert left.pid not in [record.pid for record in tick_reaper.reap(min_age_s=0)]
        assert left.poll() is None
    finally:
        left.kill()
        left.wait()
