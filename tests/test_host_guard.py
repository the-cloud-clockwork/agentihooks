import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

GUARD = Path(__file__).resolve().parents[1] / "scripts" / "host_guard"
NEEDS_HOST = pytest.mark.skipif(
    not shutil.which("jq") or not Path("/proc/self/comm").exists(), reason="guard needs jq and /proc"
)
PROBE = "import ctypes, sys, time\nctypes.CDLL(None).prctl(15, sys.argv[1].encode(), 0, 0, 0)\ntime.sleep(600)\n"


def _probe(comm: str, *command: str) -> subprocess.Popen:
    process = subprocess.Popen([sys.executable, "-c", PROBE, comm, *command])
    comm_file = Path(f"/proc/{process.pid}/comm")
    for _ in range(200):
        if comm_file.read_text().strip() == comm:
            return process
        time.sleep(0.05)
    process.kill()
    raise AssertionError(f"probe {process.pid} never took comm {comm}")


def _guard(home: Path, config: dict, comm: str) -> subprocess.CompletedProcess:
    running = subprocess.run(["ps", "-u", str(os.getuid()), "-o", "comm="], capture_output=True, text=True, check=True)
    names = {part for line in running.stdout.splitlines() if line.strip() for part in (line.strip(), line.split()[0])}
    bystanders = sorted(names - {comm})
    (home / "guard-config.json").write_text(json.dumps({**config, "excludes": config["excludes"] + bystanders}))
    return subprocess.run(
        ["bash", str(GUARD / "guard-daemon.sh")], env={**os.environ, "HOME": str(home)}, capture_output=True, text=True
    )


@NEEDS_HOST
def test_guard_skips_a_long_lived_tick_and_agent_wrapper_and_still_reaps_the_other_old_processes(tmp_path):
    config = {**json.loads((GUARD / "guard-config.json").read_text()), "threshold": 4, "timeout": "0s"}
    comm = f"ahguard{os.getpid() % 100000}"
    ticks = [
        _probe(comm, "agentihooks", "swarm", "tick"),
        _probe(comm, "-m", "scripts.swarm.scheduled_tick"),
        _probe(comm, "/home/u/.local/bin/agentihooks", "select-profile", "engineer", "--agent", "claude"),
        _probe(comm, "/home/u/.venv/bin/agentihooks", "swarm", "tick-watch"),
        _probe(comm, "/home/u/.venv/bin/agentihooks", "swarm", "prober"),
        _probe(comm, "/home/u/.venv/bin/agentihooks", "swarm", "waker"),
    ]
    others = [
        _probe(comm, "agentihooks", "ledger", "watch", "rig-grade-swarm"),
        _probe(comm, "agentihooks", "swarm", "waker-report"),
    ]
    try:
        assert _guard(tmp_path, config, comm).returncode == 0
        assert all(process.poll() is None for process in [*ticks, *others])
        assert _guard(tmp_path, config, comm).returncode == 0
        for other in others:
            other.wait(timeout=10)
        assert all(tick.poll() is None for tick in ticks)
        log = (tmp_path / "guard-daemon.log").read_text()
        for tick in ticks:
            assert f"SKIPPED pid={tick.pid} name={comm} reason=excluded_command" in log
        for other in others:
            assert f"KILLED pid={other.pid} name={comm} reason=ghost_detected count=8" in log
    finally:
        for process in [*ticks, *others]:
            process.kill()
            process.wait()


@NEEDS_HOST
@pytest.mark.parametrize(
    ("commands", "logged"),
    [(["("], "ERROR config_malformed excludeCommands pattern=("), ("tick", "ERROR config_malformed excludeCommands")],
)
def test_guard_refuses_a_malformed_command_exclude_before_reaping(tmp_path, commands, logged):
    config = {**json.loads((GUARD / "guard-config.json").read_text()), "excludeCommands": commands}
    assert _guard(tmp_path, config, "").returncode == 1
    log = (tmp_path / "guard-daemon.log").read_text()
    assert logged in log
    assert "TICK_START" not in log
