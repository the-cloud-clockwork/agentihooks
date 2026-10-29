import json
import subprocess
import sys
import time
from pathlib import Path

from scripts.scheduler.service import runtime_dir


def start_scheduler(environ):
    directory = Path(environ["HOME"]) / runtime_dir(environ).relative_to(Path.home())
    directory.mkdir(parents=True, exist_ok=True)
    log = directory / "fixture.log"
    root = Path(__file__).resolve().parents[2]
    entries = subprocess.run(
        [sys.executable, "-c", "from scripts.scheduler.core import Scheduler; print(Scheduler(None).name('entries'))"],
        cwd=root,
        env=environ,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.strip()
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
    lock = directory / "supervisor.lock"
    while time.monotonic() < deadline:
        assert process.poll() is None, log.read_text()
        if lock.exists() and lock.read_text() == str(process.pid):
            return process, entries
        time.sleep(0.01)
    process.terminate()
    process.wait(timeout=10)
    raise AssertionError(log.read_text())


def stop_scheduler(scheduler, redis):
    from scripts.events.bus import StateConflict
    from scripts.events.redis_state import RedisStateStore
    from scripts.scheduler.core import BUCKET

    process, entries = scheduler
    process.terminate()
    process.wait(timeout=10)
    state = RedisStateStore(redis, BUCKET)
    while (stored := state.get(entries)) is not None:
        document = json.loads(stored.value)
        for record in document.get("entries", {}).values():
            if record["owner"] == "process" and record["state"] in ("ready", "active", "pending"):
                record["state"] = "cancelled"
        try:
            state.update(entries, json.dumps(document).encode(), stored.revision)
            return
        except StateConflict:
            continue
