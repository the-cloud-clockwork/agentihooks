"""Where a quick swarm keeps its state: one scratchpad folder with the registry, culture, room and seat folders."""

import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

INDEX = "quick-swarms.json"
SEAT_FILES = ("plan.md", "notes.md", "learned.md", "handoff.md")


def seat_dir(folder: Path, seat: str) -> Path:
    """The folder holding one seat's plan, notes, learned notes and handoff."""
    return folder / "seats" / seat


def plan(folder: Path, seat: str) -> Path:
    """The seat's plan, one checkbox line per stage."""
    return seat_dir(folder, seat) / "plan.md"


def handoff(folder: Path, seat: str) -> Path:
    """The seat's latest Handoff v2."""
    return seat_dir(folder, seat) / "handoff.md"


def learned(folder: Path, seat: str) -> Path:
    """The lessons every later occupant of the seat reads."""
    return seat_dir(folder, seat) / "learned.md"


def opening(folder: Path, seat: str) -> Path:
    """The opening prompt the seat's latest agent was launched with."""
    return seat_dir(folder, seat) / "opening.md"


def room(folder: Path) -> Path:
    """The chat file of seats that share no native messaging channel."""
    return folder / "room" / "chat.jsonl"


def culture(folder: Path) -> Path:
    """The culture every seat of the quick swarm reads."""
    return folder / "culture.md"


def create(folder: Path, seats: Iterable[str]) -> None:
    """Make the quick folder, its room and each seat's files, leaving files that already exist untouched."""
    room(folder).parent.mkdir(parents=True, exist_ok=True)
    culture(folder).touch()
    for seat in seats:
        seat_dir(folder, seat).mkdir(parents=True, exist_ok=True)
        for name in SEAT_FILES:
            (seat_dir(folder, seat) / name).touch()


def index_path(environ: Mapping[str, str]) -> Path:
    """The host-wide index of live quick swarms, in the agentihooks home."""
    home = environ.get("AGENTIHOOKS_HOME") or Path(environ.get("HOME") or Path.home()) / ".agentihooks"
    return Path(home) / INDEX


def _index(environ: Mapping[str, str]) -> dict[str, str]:
    try:
        data: Any = json.loads(index_path(environ).read_text())
    except (OSError, ValueError):
        return {}
    try:
        items: Any = data.items()
    except AttributeError:
        return {}
    return {str(key): str(value) for key, value in items}


def _write_index(environ: Mapping[str, str], entries: dict[str, str]) -> None:
    target = index_path(environ)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{INDEX}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(entries, indent=2))
    os.replace(temporary, target)


def register(environ: Mapping[str, str], quick_id: str, folder: Path) -> None:
    """Record a live quick swarm so its master pane is found by pane id."""
    _write_index(environ, {**_index(environ), quick_id: str(folder)})


def unregister(environ: Mapping[str, str], quick_id: str) -> None:
    """Forget a stopped quick swarm."""
    _write_index(environ, {key: value for key, value in _index(environ).items() if key != quick_id})


def folders(environ: Mapping[str, str]) -> list[Path]:
    """The folders of every live quick swarm on this host, in the order they started."""
    return [Path(value) for value in _index(environ).values()]
