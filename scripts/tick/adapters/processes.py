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
        self._names: set[str] | None = None

    def live_names(self, listing: Callable[[], set[str]]) -> set[str]:
        """List the live agent names on the first read of a pass and serve a copy of that listing to later reads."""
        if self._names is None:
            self._names = set(listing())
        return set(self._names)

    def forget_names(self) -> None:
        """Drop the pass's live names listing after a change to the agents, so the next read lists again."""
        self._names = None

    def scan(self, *, deadline: Deadline) -> dict[int, Process]:
        deadline.live()
        if self._table is None:
            self._table = self._scan()
        return self._table

    def rescan(self, *, deadline: Deadline) -> dict[int, Process]:
        """Replace the pass snapshot with a fresh scan, for a process started after the first read."""
        deadline.live()
        self._table = None
        return self.scan(deadline=deadline)
