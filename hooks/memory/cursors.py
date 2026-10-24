"""Durable transcript cursors; a commit is a compare-and-set on ``revision``."""

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path


class CursorConflict(Exception):
    pass


@dataclass(frozen=True)
class Cursor:
    source: str
    device: int = 0
    inode: int = 0
    offset: int = 0
    anchor: str = ""
    fmt: str = ""
    turn: dict = field(default_factory=dict)
    revision: int = 0
    replays: int = 0


class CursorStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, source: str) -> Path:
        return self.root / f"{hashlib.sha256(source.encode()).hexdigest()}.json"

    @contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def load(self, source: str) -> Cursor:
        path = self._path(source)
        if not path.exists():
            return Cursor(source)
        return Cursor(**json.loads(path.read_text(encoding="utf-8")))

    def commit(self, cursor: Cursor) -> Cursor:
        with self._locked():
            if self.load(cursor.source).revision != cursor.revision:
                raise CursorConflict("stale_cursor")
            committed = replace(cursor, revision=cursor.revision + 1)
            path = self._path(cursor.source)
            temporary = path.with_suffix(".tmp")
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(asdict(committed), handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return committed

    def replays_total(self) -> int:
        return sum(json.loads(path.read_text(encoding="utf-8"))["replays"] for path in self.root.glob("*.json"))
