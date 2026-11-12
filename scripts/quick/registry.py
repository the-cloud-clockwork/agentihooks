"""The quick swarm registry: one locked file in the quick folder naming each seat's pane, harness and state."""

import contextlib
import dataclasses
import fcntl
import json
import os
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FILE = "seats.json"
READY = "ready"
PENDING = "handoff"
LAUNCHING = "launching"


@dataclass(frozen=True)
class Seat:
    """One seat: its column position, name, herdr pane, harness, session name, session id and state."""

    position: int
    seat: str
    pane_id: str
    harness: str
    name: str
    session_id: str = ""
    state: str = READY


@dataclass(frozen=True)
class Swarm:
    """A quick swarm: its id, the directory its agents work in and its seats in position order."""

    quick_id: str
    directory: str
    seats: tuple[Seat, ...]

    def seat(self, name: str) -> Seat | None:
        """The seat called ``name``."""
        return next((found for found in self.seats if found.seat == name), None)

    def at_pane(self, pane_id: str) -> Seat | None:
        """The seat whose herdr pane is ``pane_id``."""
        return next((found for found in self.seats if found.pane_id == pane_id), None)


def path(folder: Path) -> Path:
    """The registry file of the quick swarm kept in ``folder``."""
    return folder / FILE


def _decode(data: Any) -> Swarm:
    seats = tuple(Seat(**seat) for seat in data["seats"])
    return Swarm(str(data["quick_id"]), str(data["directory"]), seats)


def load(folder: Path) -> Swarm | None:
    """The quick swarm in ``folder``; None when it has no readable registry."""
    try:
        return _decode(json.loads(path(folder).read_text()))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save(folder: Path, swarm: Swarm) -> None:
    """Write the registry atomically through a temporary file beside it."""
    target = path(folder)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{FILE}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(dataclasses.asdict(swarm), indent=2))
    os.replace(temporary, target)


@contextlib.contextmanager
def locked(folder: Path) -> Generator[None, None, None]:
    """Hold an exclusive lock on the registry while it is read, changed and written."""
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / f".{FILE}.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _change(folder: Path, name: str, want: str | None, state: str) -> Seat | None:
    with locked(folder):
        swarm = load(folder)
        found = swarm.seat(name) if swarm else None
        if swarm is None or found is None or (want is not None and found.state != want):
            return None
        changed = dataclasses.replace(found, state=state)
        seats = tuple(changed if seat.seat == name else seat for seat in swarm.seats)
        save(folder, dataclasses.replace(swarm, seats=seats))
        return changed


def set_state(folder: Path, name: str, state: str) -> Seat | None:
    """Set the state of the seat called ``name``; None when the swarm or the seat is not there."""
    return _change(folder, name, None, state)


def take(folder: Path, name: str, want: str, then: str) -> Seat | None:
    """Move the seat from state ``want`` to ``then`` under the lock; None when it was not in ``want``."""
    return _change(folder, name, want, then)
