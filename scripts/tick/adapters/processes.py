"""Processes port: one process table scan per tick pass, shared by every step that reads it."""

from collections.abc import Callable

from hooks.proc import Process, processes
from scripts.tick.deadline import Deadline

__all__ = ["PassProcesses"]


class PassProcesses:
    """Scan the process table on the first read of a pass and serve that snapshot to every later read."""

    def __init__(self, scan: Callable[[], dict[int, Process]] = processes) -> None:
        self._scan = scan
        self._table: dict[int, Process] | None = None
        self.scans = 0

    def scan(self, *, deadline: Deadline) -> dict[int, Process]:
        if deadline.expired:
            raise TimeoutError("tool deadline expired")
        if self._table is None:
            self._table = self._scan()
            self.scans += 1
        return self._table

    def rescan(self, *, deadline: Deadline) -> dict[int, Process]:
        """Replace the pass snapshot with a fresh scan, for a process started after the first read."""
        self._table = None
        return self.scan(deadline=deadline)
