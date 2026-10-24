"""Byte-safe incremental transcript tailing.

Only newline-terminated records are decoded, so a record cut inside a UTF-8
character or a JSON value stays on disk for the next poll. A changed device or
inode, a file shorter than the committed offset, or changed bytes just before
it restart the read from offset zero and count one replay.
"""

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from hooks.memory.cursors import Cursor, CursorStore
from hooks.memory.records import TranscriptRecord, TurnState, normalize_entries
from hooks.memory.transcript_reader import detect_transcript_format

ANCHOR_BYTES = 64


@dataclass(frozen=True)
class Batch:
    records: list[TranscriptRecord]
    cursor: Cursor
    start: int
    end: int
    pending: int
    replayed: bool

    @property
    def identity(self) -> str:
        return f"{self.cursor.device}:{self.cursor.inode}:{self.start}:{self.cursor.offset}"


def _anchor(handle, offset: int) -> str:
    start = max(0, offset - ANCHOR_BYTES)
    handle.seek(start)
    return hashlib.sha256(handle.read(offset - start)).hexdigest()


def _entries(complete: bytes) -> list[object]:
    entries = []
    for raw in complete.splitlines():
        try:
            entries.append(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return entries


def read_batch(path: str | Path, cursor: Cursor) -> Batch:
    with open(path, "rb") as handle:
        stat = os.fstat(handle.fileno())
        same = (
            (stat.st_dev, stat.st_ino) == (cursor.device, cursor.inode)
            and cursor.offset <= stat.st_size
            and _anchor(handle, cursor.offset) == cursor.anchor
        )
        start = cursor.offset if same else 0
        handle.seek(start)
        data = handle.read()
        complete = data[: data.rfind(b"\n") + 1]
        offset = start + len(complete)
        anchor = _anchor(handle, offset)
    replayed = not same and cursor.revision > 0
    fmt = cursor.fmt if same else ""
    if not fmt and complete:
        fmt = detect_transcript_format(path)
    turn = TurnState.from_dict(cursor.turn if same else {})
    records = list(normalize_entries(fmt, _entries(complete), turn)) if complete else []
    proposed = replace(
        cursor,
        device=stat.st_dev,
        inode=stat.st_ino,
        offset=offset,
        anchor=anchor,
        fmt=fmt,
        turn=turn.to_dict(),
        replays=cursor.replays + replayed,
    )
    return Batch(records, proposed, start, start + len(data), len(data) - len(complete), replayed)


class Collector:
    def __init__(self, store: CursorStore):
        self.store = store

    def poll(self, path: str | Path, accept: Callable[[Batch], None]) -> Batch:
        """Read new complete records; ``accept`` must persist them before the cursor commits."""
        loaded = self.store.load(str(path))
        batch = read_batch(path, loaded)
        if batch.cursor == loaded:
            return batch
        if batch.records:
            accept(batch)
        return replace(batch, cursor=self.store.commit(batch.cursor))
