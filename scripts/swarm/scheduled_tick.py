"""The swarm minute tick as an agentihooks scheduler cron, run by a holder that keeps one tick per machine."""

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.scheduler.core import Scheduler
from scripts.scheduler.service import AlreadyRunning, machine_lock, runtime_dir

if TYPE_CHECKING:
    from scripts.events.bus import StateStore

OWNER = "agentihooks swarm tick"
EXPRESSION = "* * * * *"
JOURNAL = "agentihooks-swarm"
LIVE = ("active", "pending")
TIMEOUT_S = 540
STOP_S = 90
DELIVERED = "swarm-tick-delivered"
FRESH_S = TIMEOUT_S + STOP_S + 60
KEEP = ("HOME", "LANG", "LOGNAME", "SHELL", "USER", "XDG_RUNTIME_DIR", "XDG_DATA_DIRS")
ROOT = Path(__file__).resolve().parents[2]
LOCK = "swarm-tick"
HELD_FD = "AGENTIHOOKS_SWARM_TICK_LOCK_FD"


def ensure(scheduler: Scheduler) -> str:
    from scripts.scheduler.lease import held

    with held(scheduler.state, scheduler.name("swarm-tick-lock"), 30):
        entry_id = scheduler.read("swarm-tick")
        if entry_id and _live(scheduler, entry_id):
            return entry_id
        entry = scheduler.cron(owner=OWNER, payload="swarm tick", expression=EXPRESSION)
        entry.timeout.connect(fire)
        entry.start()
        scheduler.put("swarm-tick", entry.id)
        return entry.id


def _live(scheduler: Scheduler, entry_id: str) -> bool:
    try:
        return scheduler.get(entry_id).state in LIVE
    except KeyError:
        return False


def cancel(scheduler: Scheduler) -> None:
    for entry in scheduler.owned(OWNER):
        entry.cancel()
    scheduler.put("swarm-tick", "")


def fire(event: dict, delivery_id: str, state: "StateStore") -> None:
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


def record(scheduler: Scheduler, root: Path | str, at: float) -> None:
    scheduler.put(DELIVERED, json.dumps({"root": str(root), "at": at}))


def undelivered(scheduler: Scheduler, installed: Path | str, now: float) -> str:
    raw = scheduler.read(DELIVERED)
    if not raw:
        return "no scheduler has delivered a swarm tick yet"
    try:
        delivered = json.loads(raw)
        root, age = delivered["root"], now - delivered["at"]
        foreign = Path(root).resolve() != Path(installed).resolve()
    except (ValueError, KeyError, TypeError):
        return f"the swarm tick delivery record is unreadable: {raw}"
    if foreign:
        return f"the last swarm tick was delivered by a scheduler running {root}, not the installed {installed}"
    if age > FRESH_S:
        return f"the last swarm tick was delivered {int(age)} seconds ago"
    return ""


def delivered_at(scheduler: Scheduler) -> float:
    try:
        return float(json.loads(scheduler.read(DELIVERED))["at"])
    except (ValueError, KeyError, TypeError):
        return 0.0


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


def _inherited(environ: Mapping[str, str]) -> int | None:
    try:
        fd = int(environ[HELD_FD])
        same = os.path.samestat(os.fstat(fd), os.stat(runtime_dir() / f"{LOCK}.lock"))
    except (KeyError, ValueError, OSError):
        return None
    return fd if same else None


@contextmanager
def tick_lock(environ: Mapping[str, str] = os.environ) -> Iterator[None]:
    fd = _inherited(environ)
    if fd is None:
        with machine_lock(LOCK):
            yield
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise AlreadyRunning(f"scheduler {LOCK} is already running") from exc
    yield


def main(popen: Callable[..., subprocess.Popen] = subprocess.Popen) -> int:
    from scripts.swarm import timer

    try:
        with machine_lock(LOCK) as held:
            fd = held.fileno()
            env = {**environment(os.environ), HELD_FD: str(fd)}
            code = _finish(popen(command(timer.entry_point()), env=env, pass_fds=(fd,)))
    except AlreadyRunning:
        return 0
    if not code:
        _delivered(time.time())
    return code


def _delivered(at: float) -> None:
    from scripts.events import state

    try:
        record(Scheduler(), ROOT, at)
    except state.unavailable() as exc:
        print(f"the swarm tick delivery could not be recorded: {type(exc).__name__}: {exc}", file=sys.stderr)


def _held(name: str) -> bool:
    try:
        with machine_lock(name):
            return False
    except AlreadyRunning:
        return True


def restart(
    kill: Callable[[int, int], None] = os.kill,
    schedule: Callable[[], bool] | None = None,
    wait: Callable[[float], None] = time.sleep,
) -> str:
    """The supervisor runs the code it started from, so a restart from the installed watcher replaces stale code."""
    if _held("supervisor"):
        kill(int((runtime_dir() / "supervisor.lock").read_text()), signal.SIGTERM)
        for _ in range(STOP_S):
            if not _held("supervisor"):
                break
            wait(1)
    if schedule is None:
        from scripts.swarm import timer

        schedule = timer.schedule
    return "restarted the scheduler tick" if schedule() else "restarted the scheduler, but the tick did not register"


if __name__ == "__main__":
    raise SystemExit(main())
