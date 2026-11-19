import hashlib
import json
import os
import uuid
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

from .entry import Entry
from .fragments import document  # pyright: ignore[reportUnknownVariableType]

BEHIND = 64


def folder(environ: Mapping[str, str]) -> Path:
    """The folder of this host's entries."""
    home = environ.get("AGENTIHOOKS_HOME")
    return (Path(home) if home else Path.home() / ".agentihooks") / "ledger-entries"


def path(held: Path, key: tuple[str, str]) -> Path:
    return held / (hashlib.sha256(json.dumps(key).encode()).hexdigest()[:32] + ".entry")


def read(file: Path, generation: int) -> tuple[int, Entry] | None:
    try:
        with file.open("rb") as stream:
            held = int(stream.readline())
            if held > generation:
                return None
            state = json.loads(stream.read())
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    fragments = document(state)
    return held, Entry(held, None, fragments.value, fragments)


def write(file: Path, generation: int, text: str) -> None:
    staged = file.with_name(file.name + uuid.uuid4().hex)
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as stream:
            stream.write(str(generation) + "\n")
            stream.write(text)
        os.replace(staged, file)
    except OSError:
        with suppress(OSError):
            staged.unlink()
