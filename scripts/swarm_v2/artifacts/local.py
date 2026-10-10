"""A durable directory backend: atomic replace with fsync, every key contained in its root."""

import hashlib
import os
import tempfile
from pathlib import Path

from scripts.swarm_v2.artifacts.base import Ack, ArtifactError
from scripts.swarm_v2.filesystem import LayoutError, contain

TEMPORARY = ".partial-"


def _sync(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class LocalBackend:
    kind = "local"

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        try:
            return contain(self.root, key)
        except LayoutError as error:
            raise ArtifactError(str(error)) from error

    def write(self, key: str, data: bytes) -> Ack:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=TEMPORARY, dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        _sync(path.parent)
        return Ack(len(data), hashlib.sha256(data).hexdigest())

    def size(self, key: str) -> int | None:
        path = self._path(key)
        return path.stat().st_size if path.is_file() else None

    def read(self, key: str, start: int, length: int) -> bytes:
        with self._path(key).open("rb") as handle:
            handle.seek(start)
            return handle.read(length)

    def move(self, source: str, target: str) -> None:
        path = self._path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self._path(source), path)
        _sync(path.parent)

    def remove(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def keys(self, prefix: str) -> list[str]:
        base = self._path(prefix)
        root = self.root.resolve()
        found = base.rglob("*") if base.is_dir() else ()
        return sorted(
            path.relative_to(root).as_posix()
            for path in found
            if path.is_file() and not path.name.startswith(TEMPORARY)
        )
