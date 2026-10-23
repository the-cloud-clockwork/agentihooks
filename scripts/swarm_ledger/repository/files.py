import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from . import postgres

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS ledger_files (slug TEXT, name TEXT, data BLOB NOT NULL, "
    "touched_at INTEGER NOT NULL, PRIMARY KEY(slug,name))"
)
PUT = (
    "INSERT INTO ledger_files VALUES (?, ?, ?, ?) "
    "ON CONFLICT(slug,name) DO UPDATE SET touched_at=excluded.touched_at"
)
GET = "SELECT data FROM ledger_files WHERE slug=? AND name=?"
TOUCHED = "SELECT name, touched_at FROM ledger_files WHERE slug=?"
ALL = "SELECT name, data FROM ledger_files WHERE slug=? ORDER BY name"
DROP = "DELETE FROM ledger_files WHERE slug=? AND name=?"
PURGE = "DELETE FROM ledger_files WHERE slug=?"
SUFFIX = ".media"
CACHE = ".media-cache"
FILE_NAME = re.compile(r"[0-9a-f]{64}\.[a-z0-9]+\Z")
IRREGULAR = "ledger media must contain regular files"


class Files(Protocol):
    def put(self, slug: str, name: str, data: bytes) -> None: ...

    def get(self, slug: str, name: str) -> bytes | None: ...

    def local(self, slug: str, name: str) -> Path | None: ...

    def touched(self, slug: str) -> dict[str, float]: ...

    def drop(self, slug: str, name: str) -> None: ...

    def purge(self, slug: str) -> None: ...

    def files(self, slug: str) -> dict[str, bytes]: ...


class FolderFiles:
    def __init__(self, directory: Path):
        self.directory = directory

    def folder(self, slug: str) -> Path:
        return self.directory / f"{slug}{SUFFIX}"

    def put(self, slug: str, name: str, data: bytes) -> None:
        path = self.folder(slug) / name
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(path)
        path.touch()

    def get(self, slug: str, name: str) -> bytes | None:
        path = self.local(slug, name)
        return None if path is None else path.read_bytes()

    def local(self, slug: str, name: str) -> Path | None:
        path = self.folder(slug) / name
        if path.is_symlink():
            raise ValueError(IRREGULAR)
        return path if path.is_file() else None

    def touched(self, slug: str) -> dict[str, float]:
        return {path.name: path.stat().st_mtime for path in self.folder(slug).glob("*") if path.is_file()}

    def drop(self, slug: str, name: str) -> None:
        (self.folder(slug) / name).unlink(missing_ok=True)

    def purge(self, slug: str) -> None:
        import shutil

        shutil.rmtree(self.folder(slug), ignore_errors=True)

    def files(self, slug: str) -> dict[str, bytes]:
        folder = self.folder(slug)
        if not folder.is_dir():
            return {}
        result = {}
        for path in sorted(folder.iterdir()):
            if path.is_symlink() or not path.is_file():
                raise ValueError(IRREGULAR)
            result[path.name] = path.read_bytes()
        return result


class DatabaseFiles:
    def __init__(self, opener: Callable[[], object], cache: Path, clock: Callable[[], float] = time.time):
        self.opener, self.cache, self.clock, self.ready = opener, cache, clock, False

    def run(self, sql: str, parameters: tuple) -> list:
        connection = self.opener()
        try:
            with connection:
                if not self.ready:
                    connection.executescript(SCHEMA)
                    self.ready = True
                return list(connection.execute(sql, parameters))
        finally:
            connection.close()

    def put(self, slug: str, name: str, data: bytes) -> None:
        self.run(PUT, (slug, name, data, int(self.clock())))

    def get(self, slug: str, name: str) -> bytes | None:
        rows = self.run(GET, (slug, name))
        return bytes(rows[0][0]) if rows else None

    def local(self, slug: str, name: str) -> Path | None:
        data = self.get(slug, name)
        if data is None:
            return None
        path = self.cache / slug / name
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(path)
        return path

    def touched(self, slug: str) -> dict[str, float]:
        return {name: float(at) for name, at in self.run(TOUCHED, (slug,))}

    def drop(self, slug: str, name: str) -> None:
        self.run(DROP, (slug, name))

    def purge(self, slug: str) -> None:
        self.run(PURGE, (slug,))

    def files(self, slug: str) -> dict[str, bytes]:
        return {name: bytes(data) for name, data in self.run(ALL, (slug,))}


def shared(address: str, directory: Path) -> DatabaseFiles:
    return DatabaseFiles(lambda: postgres.Connection(address), directory / CACHE)


def build(directory: Path) -> Files:
    address = postgres.url(os.environ)
    return shared(address, directory) if address else FolderFiles(directory)


def of(repository) -> Files:
    if isinstance(repository, postgres.PostgresLedgerRepository):
        return shared(repository.address, repository.directory)
    return FolderFiles(repository.directory)


def adopt(directory: Path, store: Files) -> list[str]:
    if isinstance(store, FolderFiles):
        return []
    moved = []
    for folder in sorted(directory.glob(f"*{SUFFIX}")):
        if folder.is_symlink() or not folder.is_dir():
            continue
        slug = folder.name.removesuffix(SUFFIX)
        for path in sorted(folder.iterdir()):
            if path.is_symlink() or not path.is_file() or not FILE_NAME.match(path.name):
                continue
            store.put(slug, path.name, path.read_bytes())
            path.unlink()
            moved.append(f"{slug}/{path.name}")
    return moved
