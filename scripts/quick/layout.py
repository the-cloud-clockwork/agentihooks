"""Split the caller's herdr tab into the quick swarm's columns and name the seat at each position."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from scripts import herdr_host
from scripts.quick.guard import QuickError

DEFAULT_AGENTS = 4
MIN_AGENTS = 2
MAX_AGENTS = 5
MASTER = "master"


class Panes(Protocol):
    """The herdr pane operations the layout needs."""

    def split(self, pane_id: str, ratio: float) -> str:
        """Split ``pane_id`` to the right, ``pane_id`` keeping ``ratio`` of its width; the new pane's id."""
        ...

    def columns(self, pane_id: str) -> dict[str, tuple[int, int]]:
        """Every pane in ``pane_id``'s tab with its column start and width."""
        ...


@dataclass(frozen=True)
class HerdrPanes:
    """The live herdr server, splitting panes that open in ``directory``."""

    directory: Path
    environ: dict[str, str]

    def split(self, pane_id: str, ratio: float) -> str:
        """Split ``pane_id`` to the right, ``pane_id`` keeping ``ratio`` of its width; the new pane's id."""
        return herdr_host.split_right(pane_id, self.directory, self.environ, ratio).pane_id

    def columns(self, pane_id: str) -> dict[str, tuple[int, int]]:
        """Every pane in ``pane_id``'s tab with its column start and width."""
        return herdr_host.columns(pane_id, self.environ)


def seats(count: int) -> list[str]:
    """The master then numbered engineers for a quick swarm of ``count`` agents."""
    if not MIN_AGENTS <= count <= MAX_AGENTS:
        raise QuickError(f"a quick swarm holds {MIN_AGENTS} to {MAX_AGENTS} agents, not {count}")
    return [MASTER, *(f"eng-{number}" for number in range(1, count))]


def build(caller: str, count: int, panes: Panes) -> list[str]:
    """The caller's pane, then the new panes left to right, in ``count`` equal columns: each split keeps one share."""
    seats(count)
    owned = [caller]
    for shares in range(count, 1, -1):
        owned.append(panes.split(owned[-1], 1 / shares))
    columns = panes.columns(caller)
    return [caller, *sorted(owned[1:], key=lambda pane: columns[pane][0])]
