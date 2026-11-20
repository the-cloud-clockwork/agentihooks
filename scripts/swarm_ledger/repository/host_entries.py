import hashlib
import json
import os
import uuid
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Protocol

from .entry import Entry
from .fragments import document  # pyright: ignore[reportUnknownVariableType]

BEHIND = 64


class Kept(Protocol):
    """What a host copy stores of an entry."""

    @property
    def generation(self) -> int: ...

    @property
    def text(self) -> str: ...


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
        with os.fdopen(os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
            stream.write(str(generation).encode() + b"\n")
            stream.write(text.encode())
        os.replace(staged, file)
    except OSError:
        with suppress(OSError):
            staged.unlink()


class Copies:
    """This host's copies of the ledgers of one database, and the generation each copy was last read or written at."""

    def __init__(self, held: Path, database: str) -> None:
        self.held = held
        self.database = database
        self.generations: dict[str, int] = {}

    def adopt(self, slug: str, generation: int) -> Entry | None:
        """The copy of the ledger when it is no newer than generation."""
        copy = read(path(self.held, (self.database, slug)), generation)
        if copy is None:
            return None
        self.generations[slug] = copy[0]
        return copy[1]

    def keep(self, slug: str, entry: Kept, whole: bool) -> None:
        """Store entry as the copy after a whole load, or once the copy is BEHIND generations old."""
        if whole or entry.generation - self.generations.get(slug, entry.generation) >= BEHIND:
            write(path(self.held, (self.database, slug)), entry.generation, entry.text)
            self.generations[slug] = entry.generation

    def drop(self, slug: str) -> None:
        path(self.held, (self.database, slug)).unlink(missing_ok=True)
