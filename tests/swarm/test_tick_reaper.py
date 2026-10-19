import contextlib
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.swarm import tick_reaper

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("tick-reaper")]

ROOT = Path(__file__).resolve().parents[2]
SLEEP = "import time; time.sleep(600)"
DEAF = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(600)"
SHELL = (
    "import subprocess, sys, time; sibling = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)']); "
    "print(sibling.pid, flush=True); time.sleep(600)"
)


def _plant(code, *argv, stdout=None):
    return subprocess.Popen([sys.executable, "-c", code, *argv], start_new_session=True, stdout=stdout, text=True)


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


def test_a_planted_leftover_tick_is_reaped_and_recorded_without_touching_its_group():
    left = _plant(SHELL, "swarm", "tick", stdout=subprocess.PIPE)
    try:
        sibling = int(left.stdout.readline())
        reaped = tick_reaper.reap()
        mine = [record for record in reaped if record.pid == left.pid]
        assert len(mine) == 1
        assert mine[0].command.endswith("swarm tick")
        assert mine[0].age_s >= 0
        assert mine[0].signal == "SIGTERM"
        assert left.wait(15) == -signal.SIGTERM
        assert sibling not in [record.pid for record in reaped]
        os.kill(sibling, 0)
    finally:
        left.kill()
        left.wait()
        with contextlib.suppress(ProcessLookupError):
            os.killpg(left.pid, signal.SIGKILL)


def test_a_host_without_proc_reaps_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(tick_reaper, "PROC", tmp_path / "proc")
    assert tick_reaper.reap() == []


def test_a_leftover_that_ignores_terminate_is_killed():
    left = _plant(DEAF, "swarm", "tick", stdout=subprocess.PIPE)
    try:
        assert left.stdout.readline().strip() == "ready"
        reaped = [record for record in tick_reaper.reap(grace_s=0.5) if record.pid == left.pid]
        assert [record.signal for record in reaped] == ["SIGKILL"]
        assert left.wait(15) == -signal.SIGKILL
    finally:
        left.kill()
        left.wait()


def test_a_tick_that_started_a_moment_ago_is_reaped_too():
    young = _plant(SLEEP, "swarm", "tick")
    try:
        assert young.pid in [record.pid for record in tick_reaper.reap()]
        assert young.wait(15) == -signal.SIGTERM
    finally:
        young.kill()
        young.wait()


def test_the_reaping_tick_spares_itself_and_its_ancestors():
    inner = "from scripts.swarm import tick_reaper; print([r.pid for r in tick_reaper.reap()])"
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
        uid = os.getuid()
        monkeypatch.setattr(tick_reaper.os, "getuid", lambda: uid + 1)
        assert left.pid not in [record.pid for record in tick_reaper.reap()]
        assert left.poll() is None
    finally:
        left.kill()
        left.wait()


def test_a_tick_of_another_swarm_redis_is_never_a_leftover():
    other = {**os.environ, "AGENTIHOOKS_SWARM_REDIS_URL": "redis://127.0.0.1:6390/0"}
    left = subprocess.Popen([sys.executable, "-c", SLEEP, "swarm", "tick"], start_new_session=True, env=other)
    try:
        assert left.pid not in [record.pid for record in tick_reaper.reap()]
        assert left.poll() is None
    finally:
        left.kill()
        left.wait()


def test_a_tick_started_for_a_scratch_redis_spares_the_ticks_of_the_installed_swarm():
    installed = {**os.environ, "AGENTIHOOKS_SWARM_REDIS_URL": "redis://127.0.0.1:6379/0"}
    left = subprocess.Popen([sys.executable, "-c", SLEEP, "swarm", "tick"], start_new_session=True, env=installed)
    reaper = "from scripts.swarm import tick_reaper; print([r.pid for r in tick_reaper.reap()])"
    scratch = {**installed, "AGENTIHOOKS_SWARM_REDIS_URL": "redis://127.0.0.1:6390/0", "PYTHONPATH": str(ROOT)}
    try:
        done = subprocess.run(
            [sys.executable, "-c", reaper], cwd=ROOT, env=scratch, capture_output=True, text=True, timeout=60
        )
        assert done.returncode == 0, done.stderr
        assert str(left.pid) not in done.stdout
        assert left.poll() is None
    finally:
        left.kill()
        left.wait()
