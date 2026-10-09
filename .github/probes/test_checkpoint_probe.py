import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_swarm_v2_supervision import launch_files, runtime_directory, subprocess_environment, wait_for

FIXTURE = Path(__file__).parent / "probe_process_fixture.py"
DELAY = os.environ.get("PROBE_DELAY", "0.1")
CHECKPOINT = float(os.environ.get("PROBE_CHECKPOINT", "0.4"))
ACK = os.environ.get("PROBE_ACK", "complete")
REPEAT = int(os.environ.get("PROBE_REPEAT", "1"))


def marks(root):
    path = root / "probe-exporter.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return {row["event"]: row for row in rows}


def mtime(path):
    return path.stat().st_mtime_ns / 1e9 if path.exists() else None


def since(value, origin):
    return round(value - origin, 4) if value is not None and origin is not None else None


@pytest.mark.parametrize("index", range(REPEAT))
def test_probe_checkpoint(tmp_path, index):
    attempt, path, spec = launch_files(tmp_path)
    spec.update(
        herdr=[sys.executable, str(FIXTURE), "herdr", "server"],
        agent=[sys.executable, str(FIXTURE), "agent", "cooperative"],
        exporter=[sys.executable, str(FIXTURE), "exporter", DELAY, ACK],
        startup_seconds=3,
        quiesce_seconds=1,
        checkpoint_seconds=CHECKPOINT,
        kill_seconds=0.2,
    )
    path.write_text(json.dumps(spec))
    environment = subprocess_environment()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    began = time.time()
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from scripts.swarm_v2.supervision_runtime import main; raise SystemExit(main())",
            str(attempt),
            str(path),
        ],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    code = None
    result = {}
    root = None
    try:
        root = runtime_directory(attempt, child)
        wait_for(root / "running.json", child)
        wait_for(root / "fixture-grandchild-two.json", child)
        signalled = time.time()
        child.send_signal(signal.SIGTERM)
        result = wait_for(root / "result.json", child)
        code = child.wait(timeout=8)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        if root is not None:
            events = marks(root)
            quiesced = mtime(root / "quiesced.json")
            row = {
                "index": index,
                "worker": os.environ.get("PYTEST_XDIST_WORKER"),
                "delay": float(DELAY),
                "checkpoint": CHECKPOINT,
                "rc": code,
                **{key: result.get(key) for key in ("reason", "checkpoint_status", "quiescence", "child_exits")},
                "total": round(time.time() - began, 4),
                "drain_after_signal": since(mtime(root / "drain.json"), signalled),
                "quiesced_after_drain": since(quiesced, mtime(root / "drain.json")),
                "ack_present": (root / "exporter.checkpoint.json").exists(),
                "ack_after_quiesced": since(mtime(root / "exporter.checkpoint.json"), quiesced),
                "result_after_quiesced": since(mtime(root / "result.json"), quiesced),
                "slowest_heartbeat": events.get("seen_quiesced", {}).get("slowest_heartbeat"),
                "events": {name: since(item["t"], quiesced) for name, item in events.items()},
            }
            with open(os.environ["PROBE_DIAG"], "a") as stream:
                stream.write(json.dumps(row) + "\n")
    assert code == (0 if ACK == "complete" else 75), row
    assert result["reason"] == "termination"
    assert result["quiescence"] == "clean"
