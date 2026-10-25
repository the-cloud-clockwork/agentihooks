import ctypes
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path

from hooks.proc import _process, processes


def subreaper() -> None:
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise OSError("child reaping unavailable")


def descendants(root: int, table: dict | None = None) -> dict:
    table = processes() if table is None else table
    owned = {root}
    while True:
        found = {pid for pid, item in table.items() if item.ppid in owned}
        if found <= owned:
            return {pid: table[pid] for pid in owned - {root}}
        owned.update(found)


def living(root: int, exclude: int | None = None) -> dict:
    table = processes()
    ignored = set(descendants(exclude, table)) | {exclude} if exclude else set()
    return {pid: item for pid, item in descendants(root, table).items() if pid not in ignored and item.state != "Z"}


def send(table: dict, signum: int) -> None:
    for pid, observed in table.items():
        current = _process(pid, Path("/proc"))
        if current is not None and current.start_time == observed.start_time:
            try:
                os.kill(pid, signum)
            except ProcessLookupError:
                pass


def reap() -> list[tuple[int, int]]:
    exited = []
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return exited
        if not pid:
            return exited
        exited.append((pid, os.waitstatus_to_exitcode(status)))


def cleanup(root: int, seconds: float, observe: Callable[[], None]) -> bool:
    send(living(root), signal.SIGTERM)
    deadline = time.monotonic() + seconds
    while living(root) and time.monotonic() < deadline:
        observe()
        time.sleep(0.02)
    remaining = living(root)
    send(remaining, signal.SIGKILL)
    observe()
    return not remaining
