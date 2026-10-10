import json
import os
import signal
import subprocess
import sys
import time

from tests.test_swarm_v2_supervision import runtime_directory, wait_for, worker  # noqa: F401

ROLES = ("agent", "tool", "grandchild-one", "grandchild-two", "exporter")


def ticks(root):
    return {role: json.loads((root / f"heartbeat-{role}.json").read_text())["tick"] for role in ROLES}


def test_detach_candidate(worker):  # noqa: F811
    start, attempt, _ = worker
    child = start()
    root = runtime_directory(attempt, child)
    wait_for(root / "running.json", child)
    before = {role: wait_for(root / f"heartbeat-{role}.json", child)["tick"] for role in ROLES}
    kill = os.environ.get("PROBE_KILL")
    if kill:
        receipt = "exporter.json" if kill == "exporter" else f"fixture-{kill}.json"
        os.kill(json.loads((root / receipt).read_text())["pid"], signal.SIGKILL)
    begun = time.monotonic()
    viewer = subprocess.Popen([sys.executable, "-c", "pass"])
    assert viewer.wait() == 0
    before = ticks(root)
    deadline = begun + 5
    after = ticks(root)
    while not all(after[role] > before[role] for role in ROLES) and time.monotonic() < deadline:
        time.sleep(0.02)
        after = ticks(root)
    row = {
        "stalled": [role for role in ROLES if not after[role] > before[role]],
        "progress_s": round(time.monotonic() - begun, 4),
        "alive": child.poll() is None,
    }
    with open(os.environ["PROBE_DIAG"], "a") as stream:
        stream.write(json.dumps(row) + "\n")
    assert all(after[role] > before[role] for role in ROLES), row
    assert child.poll() is None
