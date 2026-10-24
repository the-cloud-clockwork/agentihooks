"""Execution-local transcript spool; only ``acknowledged`` sequences are durable beyond this disk."""

import fcntl
import hashlib
import json
import os
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from hooks.memory.collector import Batch

RECENT_IDENTITIES = 4096


class SpoolFull(Exception):
    pass


@dataclass(frozen=True)
class SpoolConfig:
    root: Path
    max_bytes: int
    warn_bytes: int


@dataclass(frozen=True)
class Entry:
    sequence: int
    identity: str
    created: float
    records: list
    size: int


@dataclass(frozen=True)
class Status:
    level: str
    buffered: int
    settled: int
    acknowledged: int
    pending: list[int]
    corrupt: list[int]
    lost: list[int]
    transcript_spool_bytes: int
    archive_lag_seconds: float


@dataclass(frozen=True)
class FlushResult:
    uploaded: list[int] = field(default_factory=list)
    lost: list[int] = field(default_factory=list)
    error: str = ""


def _checksum(sequence: int, identity: str, created: float, records: list) -> str:
    body = json.dumps([sequence, identity, created, records], sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


def _key(identity: str) -> str:
    return hashlib.sha256(identity.encode()).hexdigest()[:16]


def _sequence(path: Path) -> int:
    return int(path.name[:20])


def _sync_dir(path: Path) -> None:
    directory = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _write_durably(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with open(temporary, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    _sync_dir(path.parent)


class Spool:
    def __init__(self, config: SpoolConfig, clock: Callable[[], float] = time.time):
        self.config = config
        self.clock = clock
        self.batches = Path(config.root) / "batches"
        self.quarantine = Path(config.root) / "lost"
        self.ack_path = Path(config.root) / "ack.json"

    @contextmanager
    def _locked(self, name: str):
        self.batches.mkdir(parents=True, exist_ok=True)
        with open(Path(self.config.root) / f".{name}.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _ack(self) -> dict:
        if not self.ack_path.exists():
            return {"settled": 0, "lost": [], "recent": {}}
        return json.loads(self.ack_path.read_bytes())

    def _files(self) -> list[Path]:
        settled = self._ack()["settled"]
        return [path for path in sorted(self.batches.glob("*.json")) if _sequence(path) > settled]

    def _read(self, path: Path) -> Entry | None:
        try:
            data = json.loads(path.read_bytes())
            entry = Entry(data["sequence"], data["identity"], data["created"], data["records"], path.stat().st_size)
        except (ValueError, KeyError, TypeError):
            return None
        checksum = _checksum(entry.sequence, entry.identity, entry.created, entry.records)
        if entry.sequence != _sequence(path) or data.get("sha256") != checksum:
            return None
        return entry

    def append(self, identity: str, records: list) -> Entry | None:
        """Returns the pending entry for a buffered identity, and None for one already acknowledged."""
        with self._locked("spool"):
            ack = self._ack()
            if _key(identity) in ack["recent"]:
                return None
            files = self._files()
            for path in files:
                if path.name.endswith(f"-{_key(identity)}.json") and (entry := self._read(path)):
                    return entry
            sequence = max([_sequence(path) for path in files] + [ack["settled"]]) + 1
            created = self.clock()
            body = {"sequence": sequence, "identity": identity, "created": created, "records": records}
            body["sha256"] = _checksum(sequence, identity, created, records)
            data = json.dumps(body, ensure_ascii=False).encode()
            if sum(path.stat().st_size for path in files) + len(data) > self.config.max_bytes:
                raise SpoolFull("spool_full")
            path = self.batches / f"{sequence:020d}-{_key(identity)}.json"
            _write_durably(path, data)
            return Entry(sequence, identity, created, records, len(data))

    def accept(self, batch: Batch) -> None:
        self.append(batch.identity, list(batch.records))

    def status(self) -> Status:
        with self._locked("spool"):
            ack = self._ack()
            files = self._files()
            entries = {_sequence(path): self._read(path) for path in files}
            size = sum(path.stat().st_size for path in files)
        created = [entry.created for entry in entries.values() if entry]
        level = "full" if size >= self.config.max_bytes else "warning" if size >= self.config.warn_bytes else "ok"
        return Status(
            level=level,
            buffered=max([*entries, ack["settled"]]),
            settled=ack["settled"],
            acknowledged=min(ack["lost"]) - 1 if ack["lost"] else ack["settled"],
            pending=[sequence for sequence, entry in entries.items() if entry],
            corrupt=[sequence for sequence, entry in entries.items() if not entry],
            lost=list(ack["lost"]),
            transcript_spool_bytes=size,
            archive_lag_seconds=max(0.0, self.clock() - min(created)) if created else 0.0,
        )

    def _acknowledge(self, path: Path, lost: bool) -> None:
        ack = self._ack()
        sequence = _sequence(path)
        if sequence <= ack["settled"]:
            return
        recent = ack["recent"] if lost else {**ack["recent"], path.stem[21:]: sequence}
        ack = {
            "settled": sequence,
            "lost": ack["lost"] + ([sequence] if lost else []),
            "recent": dict(list(recent.items())[-RECENT_IDENTITIES:]),
        }
        _write_durably(self.ack_path, json.dumps(ack).encode())

    def _settle(self, path: Path, lost: bool) -> None:
        with self._locked("spool"):
            self._acknowledge(path, lost)
            if lost:
                self.quarantine.mkdir(exist_ok=True)
                path.replace(self.quarantine / path.name)
                _sync_dir(self.quarantine)
            else:
                path.unlink()
            _sync_dir(self.batches)

    def flush(self, upload: Callable[[Entry], None]) -> FlushResult:
        """``upload`` returns only after central durable acceptance and must be idempotent on ``identity``."""
        uploaded, lost = [], []
        with self._locked("flush"):
            for path in self._files():
                entry = self._read(path)
                if entry is not None:
                    try:
                        upload(entry)
                    except Exception as error:
                        return FlushResult(uploaded, lost, type(error).__name__)
                self._settle(path, lost=entry is None)
                (uploaded if entry else lost).append(_sequence(path))
        return FlushResult(uploaded, lost)


def run_flusher(spool: Spool, upload: Callable[[Entry], None], stop: threading.Event, interval: float) -> FlushResult:
    """Flush every ``interval`` seconds until ``stop`` is set, then once more for controlled shutdown."""
    while not stop.wait(interval):
        spool.flush(upload)
    return spool.flush(upload)
