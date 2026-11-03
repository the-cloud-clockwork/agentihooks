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
SWEEP_S = 10
UNIT_FLAG = "--unit"
UNIT_TIMEOUT_S = TIMEOUT_S + STOP_S + 2 * SWEEP_S + 60
DELIVERED = "swarm-tick-delivered"
FRESH_S = TIMEOUT_S + STOP_S + 60
KEEP = ("HOME", "LANG", "LOGNAME", "SHELL", "USER", "XDG_RUNTIME_DIR", "XDG_DATA_DIRS")
ROOT = Path(__file__).resolve().parents[2]
LOCK = "swarm-tick"
HELD_FD = "AGENTIHOOKS_SWARM_TICK_LOCK_FD"
PROC = Path("/proc")


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


def _ended(tick: subprocess.Popen, killpg: Callable[[int, int], None]) -> tuple[int, bool]:
    try:
        return tick.wait(TIMEOUT_S), False
    except subprocess.TimeoutExpired:
        _signal_group(tick.pid, signal.SIGTERM, killpg)
    try:
        return tick.wait(STOP_S), True
    except subprocess.TimeoutExpired:
        _signal_group(tick.pid, signal.SIGKILL, killpg)
        return tick.wait(), True


def _signal_group(pgid: int, number: int, killpg: Callable[[int, int], None]) -> None:
    try:
        killpg(pgid, number)
    except ProcessLookupError:
        pass


def _group_alive(pgid: int) -> bool:
    """True while any process other than a zombie still sits in the process group, read from /proc."""
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if fields[2] == str(pgid) and fields[0] != "Z":
            return True
    return False


def _sweep(pgid: int, killpg: Callable[[int, int], None], alive: Callable[[int], bool]) -> None:
    """End every process left in a pass's group: SIGTERM, then SIGKILL after the sweep grace."""
    for number in (signal.SIGTERM, signal.SIGKILL):
        if not alive(pgid):
            return
        _signal_group(pgid, number, killpg)
        deadline = time.monotonic() + SWEEP_S
        while time.monotonic() < deadline:
            if not alive(pgid):
                return
            time.sleep(0.2)


def _finish(
    tick: subprocess.Popen,
    killpg: Callable[[int, int], None] = os.killpg,
    alive: Callable[[int], bool] = _group_alive,
) -> int:
    """Wait for the tick pass, and after a timed out or failed pass end every child left in its process group."""
    code, timed_out = _ended(tick, killpg)
    if code or timed_out:
        _sweep(tick.pid, killpg, alive)
    return code


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


def main(popen: Callable[..., subprocess.Popen] = subprocess.Popen, record: bool = True) -> int:
    """Hold the machine tick lock around one tick pass; the systemd fallback unit passes record=False."""
    from scripts.swarm import timer

    try:
        with machine_lock(LOCK) as held:
            fd = held.fileno()
            env = {**environment(os.environ), HELD_FD: str(fd)}
            code = _finish(popen(command(timer.entry_point()), env=env, pass_fds=(fd,), start_new_session=True))
    except AlreadyRunning:
        return 0
    if record:
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
    """The supervisor runs the code it started from, so a restart from the installed watcher replaces stale code.

    A tick in flight proves the scheduler delivers, so the restart leaves it to finish its pass.
    """
    if _held(LOCK):
        return "left the scheduler alone: a swarm tick is still running"
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
    raise SystemExit(main(record=UNIT_FLAG not in sys.argv[1:]))
