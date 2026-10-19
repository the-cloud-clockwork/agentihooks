"""Leftover swarm tick processes from earlier passes, ended by the tick that holds the machine tick lock."""

import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from scripts.scheduler.service import runtime_dir

PROC = Path("/proc")
GRACE_S = 10
HOLDER = ["-m", "scripts.swarm.scheduled_tick"]


@dataclass(frozen=True)
class Reaped:
    pid: int
    age_s: int
    command: str
    signal: str


def is_tick(argv: list[str]) -> bool:
    return argv[-2:] == ["swarm", "tick"] or argv[1:] == HOLDER


def _stat(pid: int) -> list[str]:
    return (PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()


def _argv(pid: int) -> list[str]:
    return (PROC / str(pid) / "cmdline").read_bytes().decode(errors="replace").split("\0")[:-1]


def ancestors() -> set[int]:
    pids, pid = set(), os.getpid()
    while pid > 1 and pid not in pids:
        pids.add(pid)
        try:
            pid = int(_stat(pid)[1])
        except (OSError, IndexError, ValueError):
            break
    return pids


def _age_s(pid: int) -> float:
    uptime = float((PROC / "uptime").read_text().split()[0])
    return uptime - int(_stat(pid)[19]) / os.sysconf("SC_CLK_TCK")


def alive(pid: int) -> bool:
    try:
        return _stat(pid)[0] != "Z"
    except OSError:
        return False


def _signal(pid: int, number: int) -> None:
    try:
        os.kill(pid, number)
    except ProcessLookupError:
        pass


def _environ(pid: int) -> dict[str, str]:
    raw = (PROC / str(pid) / "environ").read_bytes().decode(errors="replace")
    return dict(item.partition("=")[::2] for item in raw.split("\0") if "=" in item)


def _leftovers() -> list[tuple[int, float, list[str], str]]:
    import hooks.config  # noqa: F401  loads the env files a tick reads its swarm Redis from

    spared, uid, found = ancestors(), os.getuid(), []
    started = _environ(os.getpid())
    loaded = {key: value for key, value in os.environ.items() if key not in started}
    mine = runtime_dir(os.environ)
    for entry in sorted(PROC.iterdir()):
        if not entry.name.isdigit() or int(entry.name) in spared:
            continue
        pid = int(entry.name)
        try:
            if entry.stat().st_uid != uid or not is_tick(argv := _argv(pid)):
                continue
            if runtime_dir({**loaded, **_environ(pid)}) != mine:
                continue
            age, start = _age_s(pid), _stat(pid)[19]
        except (OSError, IndexError, ValueError):
            continue
        found.append((pid, age, argv, start))
    return found


def _wait(pids: list[int], grace_s: float) -> list[int]:
    deadline = time.monotonic() + grace_s
    while (living := [pid for pid in pids if alive(pid)]) and time.monotonic() < deadline:
        time.sleep(0.1)
    return living


def _same(pid: int, start: str) -> bool:
    try:
        return _stat(pid)[19] == start
    except (OSError, IndexError):
        return False


def reap(grace_s: float = GRACE_S) -> list[Reaped]:
    if not PROC.is_dir():
        return []
    found = _leftovers()
    for pid, _, _, _ in found:
        _signal(pid, signal.SIGTERM)
    living = set(_wait([pid for pid, _, _, _ in found], grace_s))
    killed = {pid for pid, _, _, start in found if pid in living and _same(pid, start)}
    for pid in killed:
        _signal(pid, signal.SIGKILL)
    return [
        Reaped(pid, int(age), " ".join(argv), "SIGKILL" if pid in killed else "SIGTERM") for pid, age, argv, _ in found
    ]
