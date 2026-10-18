"""The swarm minute tick as an agentihooks scheduler cron, run by a holder that keeps one tick per machine."""

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.scheduler.core import Scheduler
from scripts.scheduler.service import AlreadyRunning, machine_lock, runtime_dir

if TYPE_CHECKING:
    from redis import Redis

OWNER = "agentihooks swarm tick"
EXPRESSION = "* * * * *"
JOURNAL = "agentihooks-swarm"
LIVE = ("active", "pending")
TIMEOUT_S = 540
STOP_S = 90
KEEP = ("HOME", "LANG", "LOGNAME", "SHELL", "USER", "XDG_RUNTIME_DIR", "XDG_DATA_DIRS")
ROOT = Path(__file__).resolve().parents[2]


def ensure(scheduler: Scheduler) -> str:
    redis, key = scheduler.redis, scheduler.key("swarm-tick")
    with redis.lock(scheduler.key("swarm-tick-lock"), timeout=30, blocking_timeout=30):
        entry_id = redis.get(key)
        if entry_id and _live(scheduler, entry_id):
            return entry_id
        entry = scheduler.cron(owner=OWNER, payload="swarm tick", expression=EXPRESSION)
        entry.timeout.connect(fire)
        entry.start()
        redis.set(key, entry.id)
        return entry.id


def _live(scheduler: Scheduler, entry_id: str) -> bool:
    try:
        return scheduler.get(entry_id).state in LIVE
    except KeyError:
        return False


def fire(event: dict, delivery_id: str, redis: "Redis") -> None:
    directory = runtime_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "swarm-tick.log").open("ab") as log:
        subprocess.Popen(
            [sys.executable, "-m", "scripts.swarm.scheduled_tick"],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )


def command(binary: str) -> list[str]:
    tick = [binary, "swarm", "tick"]
    journal = shutil.which("systemd-cat")
    return [journal, "-t", JOURNAL, *tick] if journal else tick


def environment(environ: Mapping[str, str]) -> dict[str, str]:
    home = environ["HOME"]
    kept = {name: environ[name] for name in KEEP if name in environ}
    return {**kept, "PATH": f"{home}/.local/bin:{home}/.cargo/bin:/usr/local/bin:/usr/bin:/bin"}


def _finish(tick: subprocess.Popen) -> int:
    try:
        return tick.wait(TIMEOUT_S)
    except subprocess.TimeoutExpired:
        tick.terminate()
    try:
        return tick.wait(STOP_S)
    except subprocess.TimeoutExpired:
        tick.kill()
        return tick.wait()


def main(popen: Callable[..., subprocess.Popen] = subprocess.Popen) -> int:
    from scripts.swarm import timer

    try:
        with machine_lock("swarm-tick"):
            return _finish(popen(command(timer.entry_point()), env=environment(os.environ)))
    except AlreadyRunning:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
