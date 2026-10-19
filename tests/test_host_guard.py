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


def _guard(home: Path) -> None:
    subprocess.run(["bash", str(GUARD / "guard-daemon.sh")], env={**os.environ, "HOME": str(home)}, check=True)


@pytest.mark.skipif(not shutil.which("jq") or not Path("/proc/self/comm").exists(), reason="guard needs jq and /proc")
def test_guard_skips_a_long_lived_tick_and_still_reaps_the_other_old_processes(tmp_path):
    config = json.loads((GUARD / "guard-config.json").read_text())
    (tmp_path / "guard-config.json").write_text(json.dumps({**config, "threshold": 3, "timeout": "0s"}))
    comm = f"ahguard{os.getpid() % 100000}"
    tick = _probe(comm, "agentihooks", "swarm", "tick")
    others = [
        _probe(comm, "agentihooks", "ledger", "watch", "rig-grade-swarm"),
        _probe(comm, "agentihooks", "swarm", "tick-watch"),
    ]
    try:
        _guard(tmp_path)
        assert tick.poll() is None and all(other.poll() is None for other in others)
        _guard(tmp_path)
        for other in others:
            other.wait(timeout=10)
        assert tick.poll() is None
        log = (tmp_path / "guard-daemon.log").read_text()
        assert f"SKIPPED pid={tick.pid} name={comm} reason=excluded_command" in log
        for other in others:
            assert f"KILLED pid={other.pid} name={comm} reason=ghost_detected count=3" in log
    finally:
        for process in [tick, *others]:
            process.kill()
            process.wait()
