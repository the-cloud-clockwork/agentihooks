"""The quick swarm room: a file of chat lines between seats that do not share a native messaging channel."""

import fcntl
import json
from pathlib import Path
from typing import Any

from scripts.quick import paths

Line = dict[str, Any]


def post(folder: Path, sender: str, to: str, text: str, now: int) -> Line:
    """Append one line from ``sender`` to ``to`` under the room's lock."""
    line: Line = {"at": now, "from": sender, "to": to, "text": text}
    target = paths.room(folder)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(json.dumps(line) + "\n")
    return line


def _lines(folder: Path) -> list[Line]:
    try:
        raw = paths.room(folder).read_text().splitlines()
    except OSError:
        return []
    return [line for line in map(paths.json_object, raw) if line]


def _cursor(folder: Path, seat: str) -> Path:
    return paths.seat_dir(folder, seat) / "room.cursor"


def _read_at(folder: Path, seat: str) -> int:
    try:
        return int(_cursor(folder, seat).read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def _mine(folder: Path, seat: str) -> tuple[list[Line], int]:
    lines = _lines(folder)
    return [line for line in lines[_read_at(folder, seat) :] if line.get("to") == seat], len(lines)


def waiting(folder: Path, seat: str) -> int:
    """How many lines sent to ``seat`` it has not read yet."""
    return len(_mine(folder, seat)[0])


def unread(folder: Path, seat: str) -> list[Line]:
    """The lines sent to ``seat`` since it last read, marking them read."""
    mine, total = _mine(folder, seat)
    cursor = _cursor(folder, seat)
    cursor.parent.mkdir(parents=True, exist_ok=True)
    cursor.write_text(str(total))
    return mine
