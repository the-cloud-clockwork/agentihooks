"""Execution-local transcript spool; only ``acknowledged`` sequences are durable beyond this disk."""

import fcntl
import hashlib
import json
import os
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path


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
    acknowledged: int
    pending: list[int]
    lost: list[int]
    transcript_spool_bytes: int
    archive_lag_seconds: float


@dataclass(frozen=True)
class FlushResult:
    uploaded: list[int] = field(default_factory=list)
    lost: list[int] = field(default_factory=list)
    error: str = ""


def _checksum(sequence: int, identity: str, created: float, records: list) -> str:
    body = json.dumps([sequence, identity, created, records], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()


def _key(identity: str) -> str:
    return hashlib.sha256(identity.encode()).hexdigest()[:16]


def _sequence(path: Path) -> int:
    return int(path.name.partition("-")[0])


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
            return {"acknowledged": 0, "lost": []}
        return json.loads(self.ack_path.read_bytes())

    def _files(self) -> list[Path]:
        acknowledged = self._ack()["acknowledged"]
        files = sorted(self.batches.glob("*.json"), key=_sequence) if self.batches.exists() else []
        return [path for path in files if _sequence(path) > acknowledged]

    def _bytes(self) -> int:
        return sum(path.stat().st_size for path in self._files())

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

    def append(self, identity: str, records: list) -> Entry:
        with self._locked("append"):
            files = self._files()
            for path in files:
                if path.name.endswith(f"-{_key(identity)}.json") and (entry := self._read(path)):
                    return entry
            sequence = max([_sequence(path) for path in files] + [self._ack()["acknowledged"]]) + 1
            created = self.clock()
            body = {"sequence": sequence, "identity": identity, "created": created, "records": records}
            body["sha256"] = _checksum(sequence, identity, created, records)
            data = json.dumps(body, sort_keys=True, ensure_ascii=False).encode()
            if sum(path.stat().st_size for path in files) + len(data) > self.config.max_bytes:
                raise SpoolFull("spool_full")
            path = self.batches / f"{sequence:020d}-{_key(identity)}.json"
            _write_durably(path, data)
            return Entry(sequence, identity, created, records, len(data))

    def accept(self, batch) -> None:
        self.append(batch.identity, list(batch.records))

    def status(self) -> Status:
        ack = self._ack()
        files = self._files()
        entries = [self._read(path) for path in files]
        created = [entry.created for entry in entries if entry]
        size = sum(path.stat().st_size for path in files)
        level = "full" if size >= self.config.max_bytes else "warning" if size >= self.config.warn_bytes else "ok"
        return Status(
            level=level,
            buffered=max([_sequence(path) for path in files] + [ack["acknowledged"]]),
            acknowledged=ack["acknowledged"],
            pending=[_sequence(path) for path in files],
            lost=list(ack["lost"]),
            transcript_spool_bytes=size,
            archive_lag_seconds=max(0.0, self.clock() - min(created)) if created else 0.0,
        )

    def _acknowledge(self, sequence: int, lost: bool) -> None:
        ack = self._ack()
        if sequence <= ack["acknowledged"]:
            return
        ack = {"acknowledged": sequence, "lost": ack["lost"] + [sequence] * lost}
        _write_durably(self.ack_path, json.dumps(ack).encode())

    def _quarantine(self, path: Path) -> None:
        self.quarantine.mkdir(parents=True, exist_ok=True)
        path.replace(self.quarantine / path.name)
        self._acknowledge(_sequence(path), lost=True)

    def flush(self, upload: Callable[[Entry], None]) -> FlushResult:
        """``upload`` returns only after central durable acceptance and must be idempotent on ``identity``."""
        uploaded, lost = [], []
        with self._locked("flush"):
            for path in self._files():
                entry = self._read(path)
                if entry is None:
                    self._quarantine(path)
                    lost.append(_sequence(path))
                    continue
                try:
                    upload(entry)
                except Exception as error:
                    return FlushResult(uploaded, lost, type(error).__name__)
                self._acknowledge(entry.sequence, lost=False)
                path.unlink()
                uploaded.append(entry.sequence)
        return FlushResult(uploaded, lost)


def run_flusher(spool: Spool, upload: Callable[[Entry], None], stop, interval: float) -> FlushResult:
    """Flush every ``interval`` seconds until ``stop`` is set, then once more for controlled shutdown."""
    while not stop.wait(interval):
        spool.flush(upload)
    return spool.flush(upload)
