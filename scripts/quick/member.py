"""Which quick swarm seat a session occupies: by its launch variables, else by its herdr pane."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from scripts.quick import paths, registry

QUICK_ENV = "AGENTIHOOKS_QUICK_SWARM"
SEAT_ENV = "AGENTIHOOKS_QUICK_SEAT"
DIR_ENV = "AGENTIHOOKS_QUICK_DIR"


@dataclass(frozen=True)
class Member:
    """A session's seat in a live quick swarm and the folder that swarm keeps its state in."""

    folder: Path
    swarm: registry.Swarm
    seat: registry.Seat


def _by_variables(environ: Mapping[str, str]) -> Member | None:
    folder, name = environ.get(DIR_ENV), environ.get(SEAT_ENV)
    swarm = registry.load(Path(folder)) if folder and name else None
    seat = swarm.seat(str(name)) if swarm else None
    return Member(Path(str(folder)), swarm, seat) if swarm and seat else None


def _by_pane(environ: Mapping[str, str]) -> Member | None:
    pane = environ.get("HERDR_PANE_ID")
    for folder in paths.folders(environ) if pane else []:
        swarm = registry.load(folder)
        seat = swarm.at_pane(str(pane)) if swarm else None
        if swarm and seat:
            return Member(folder, swarm, seat)
    return None


def find(environ: Mapping[str, str]) -> Member | None:
    """This session's quick seat: by its launch variables, else by its herdr pane in a registered quick swarm."""
    return _by_variables(environ) or _by_pane(environ)
