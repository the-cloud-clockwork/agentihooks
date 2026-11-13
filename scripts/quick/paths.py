"""Where a quick swarm keeps its state: one scratchpad folder with the registry, culture, room and seat folders."""

import contextlib
import fcntl
import json
import os
import re
from collections.abc import Generator, Iterable, Mapping
from pathlib import Path
from typing import Any

from scripts import herdr_host
from scripts.quick.guard import QuickError

INDEX = "quick-swarms.json"
SEAT_FILES = ("plan.md", "notes.md", "learned.md", "handoff.md")


def quick_id(environ: Mapping[str, str]) -> str:
    """The quick swarm id, built from the session that starts it as master."""
    session = re.sub(r"[^0-9a-f]", "", environ.get("CLAUDE_CODE_SESSION_ID", "").lower())
    if len(session) < 8:
        raise QuickError("no session id: a quick swarm is named after the session that starts it as master")
    return f"quick-{session[:8]}"


def folder_for(environ: Mapping[str, str], quick: str, directory: Path) -> Path:
    """The scratchpad folder of quick swarm ``quick`` working in ``directory``."""
    return Path(environ.get("HOME") or Path.home()) / "scratchpad" / herdr_host.repo_label(directory) / quick


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


def read_text(path: Path) -> str:
    """The file's text; empty when it is missing or unreadable."""
    try:
        return path.read_text()
    except OSError:
        return ""


def json_object(text: str) -> dict[str, Any]:
    """The JSON object in ``text``; empty when it is not JSON or not an object."""
    try:
        data: Any = json.loads(text)
        items: Any = data.items()
    except (ValueError, AttributeError):
        return {}
    return {str(key): value for key, value in items}


def read_object(path: Path) -> dict[str, Any]:
    """The JSON object stored in ``path``; empty when it is missing or unreadable."""
    return json_object(read_text(path))


def home(environ: Mapping[str, str]) -> Path:
    """The agentihooks home folder."""
    return Path(environ.get("AGENTIHOOKS_HOME") or Path(environ.get("HOME") or Path.home()) / ".agentihooks")


def index_path(environ: Mapping[str, str]) -> Path:
    """The host-wide index of live quick swarms, in the agentihooks home."""
    return home(environ) / INDEX


@contextlib.contextmanager
def host_locked(environ: Mapping[str, str]) -> Generator[None, None, None]:
    """Hold the host-wide quick swarm lock while the index or the cron table is read and rewritten."""
    target = index_path(environ)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.with_name(f".{INDEX}.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _index(environ: Mapping[str, str]) -> dict[str, str]:
    return {key: str(value) for key, value in read_object(index_path(environ)).items()}


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
