import fcntl
import json
import subprocess
import sys
import time
from pathlib import Path

from scripts.scheduler.core import Scheduler
from scripts.scheduler.service import runtime_dir


def start_scheduler(environ):
    directory = Path(environ["HOME"]) / runtime_dir(environ).relative_to(Path.home())
    directory.mkdir(parents=True, exist_ok=True)
    log = directory / "fixture.log"
    with log.open("wb") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "scripts.scheduler.service", "supervise"],
            cwd=Path(__file__).resolve().parents[2],
            env=environ,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
        )
    deadline = time.monotonic() + 10
    with (directory / "supervisor.lock").open("a+") as handle:
        while time.monotonic() < deadline:
            assert process.poll() is None, log.read_text()
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return process
            fcntl.flock(handle, fcntl.LOCK_UN)
            time.sleep(0.01)
    process.terminate()
    process.wait(timeout=10)
    raise AssertionError(log.read_text())


def stop_scheduler(process, redis):
    process.terminate()
    process.wait(timeout=10)
    for record in Scheduler(redis).records():
        if record["owner"] == "process":
            redis.lpush(json.loads(record["payload"])["key"], "cancelled")
