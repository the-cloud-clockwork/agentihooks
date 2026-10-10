import json
import os
import subprocess
import sys
import time

from tests.test_swarm_v2_supervision import runtime_directory, wait_for, worker  # noqa: F401

ROLES = ("agent", "tool", "grandchild-one", "grandchild-two", "exporter")


def test_detach_probe(worker):  # noqa: F811
    start, attempt, _ = worker
    child = start()
    root = runtime_directory(attempt, child)
    wait_for(root / "running.json", child)
    before = {role: wait_for(root / f"heartbeat-{role}.json", child)["tick"] for role in ROLES}
    read_before = time.monotonic()
    viewer = subprocess.Popen([sys.executable, "-c", "pass"])
    assert viewer.wait() == 0
    viewer_done = time.monotonic()
    time.sleep(0.1)
    after = {role: json.loads((root / f"heartbeat-{role}.json").read_text())["tick"] for role in ROLES}
    read_after = time.monotonic()
    stalled = [role for role in ROLES if not after[role] > before[role]]
    row = {
        "worker": os.environ.get("PYTEST_XDIST_WORKER"),
        "stalled": stalled,
        "viewer_s": round(viewer_done - read_before, 4),
        "window_s": round(read_after - read_before, 4),
        "age_before_s": {role: round(read_before - before[role], 4) for role in ROLES},
        "age_after_s": {role: round(read_after - after[role], 4) for role in ROLES},
        "alive": child.poll() is None,
    }
    with open(os.environ["PROBE_DIAG"], "a") as stream:
        stream.write(json.dumps(row) + "\n")
    assert not stalled, row
    assert child.poll() is None
