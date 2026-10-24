"""One active writer per ledger folder: a flock lease held for the process lifetime, verified snapshots and restores."""

import argparse
import fcntl
import json
import os
import socket
import sqlite3
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path

LOCK = ".ledger-writer.lock"
CONFLICTS = ".ledger-writer-conflicts.jsonl"
DATABASE = "ledgers.sqlite3"


class WriterConflict(RuntimeError):
    def __init__(self, directory: Path, holder: dict):
        self.directory, self.holder = directory, holder
        super().__init__(
            f"ledger folder {directory} already has an active writer, pid {holder.get('pid')} on "
            f"{holder.get('host')}; a second writer is refused"
        )


def owner() -> dict:
    return {"pid": os.getpid(), "host": socket.gethostname(), "started_at": int(time.time() * 1000)}


def holder(directory: Path) -> dict:
    try:
        found = json.loads((Path(directory) / LOCK).read_text())
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def record_conflict(directory: Path, held: dict, claimant: dict) -> None:
    line = json.dumps({"holder": held.get("pid"), "claimant": claimant.get("pid"), "at": claimant.get("started_at")})
    with (Path(directory) / CONFLICTS).open("a") as log:
        log.write(line + "\n")


def conflicts_total(directory: Path) -> int:
    try:
        return len((Path(directory) / CONFLICTS).read_text().splitlines())
    except FileNotFoundError:
        return 0


class WriterLease:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self._fd = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, claimant: dict) -> "WriterLease":
        self.directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.directory / LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            found = holder(self.directory)
            record_conflict(self.directory, found, claimant)
            raise WriterConflict(self.directory, found) from None
        record = json.dumps(claimant, sort_keys=True).encode()
        os.pwrite(fd, record, 0)
        os.ftruncate(fd, len(record))
        os.fsync(fd)
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def existing(path: Path) -> sqlite3.Connection:
    if not Path(path).is_file():
        raise FileNotFoundError(f"no database at {path}")
    return sqlite3.connect(path)


def verify(path: Path) -> None:
    connection = existing(path)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()
    if result != "ok":
        raise ValueError(f"{path} failed its integrity check: {result}")


def copy_database(source: Path, target: Path) -> None:
    reader = existing(source)
    try:
        writer = sqlite3.connect(target)
        try:
            reader.backup(writer)
        finally:
            writer.close()
    finally:
        reader.close()


def snapshot(database: Path, target: Path) -> Path:
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    os.close(fd)
    try:
        copy_database(database, Path(temporary))
        verify(Path(temporary))
        os.replace(temporary, target)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
    return target


def restore(backup: Path, directory: Path) -> Path:
    verify(backup)
    lease = WriterLease(directory).acquire(owner())
    try:
        database = Path(directory) / DATABASE
        copy_database(backup, database)
    finally:
        lease.release()
    return database


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=Path(os.environ.get("LEDGER_DIR", "~/development-ledger")))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("snapshot").add_argument("target")
    commands.add_parser("restore").add_argument("backup")
    commands.add_parser("holder")
    args = parser.parse_args(argv)
    directory = args.dir.expanduser()
    try:
        if args.command == "snapshot":
            print(snapshot(directory / DATABASE, args.target))
        elif args.command == "restore":
            print(restore(Path(args.backup), directory))
        else:
            print(json.dumps({"holder": holder(directory), "conflicts": conflicts_total(directory)}))
    except (WriterConflict, OSError, ValueError, sqlite3.Error) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
