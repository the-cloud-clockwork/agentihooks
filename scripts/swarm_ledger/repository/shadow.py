import fcntl
from contextlib import contextmanager
from pathlib import Path

from . import bin_storage
from .rows import encode
from .sqlite import SQLiteLedgerRepository


@contextmanager
def storage_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".ledger-storage.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def persist(directory: Path, slug: str, state: dict) -> None:
    stat = (directory / f"{slug}.json").stat()
    signature = encode((stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size))
    SQLiteLedgerRepository(directory / "ledger-shadow.sqlite3").import_document(
        slug, state, bin_storage.entries().get(slug), bin_storage.restored().get(slug), signature
    )
