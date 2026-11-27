import dataclasses
import json
import re
import sqlite3
import sys
import time
from collections.abc import Callable
from typing import Any, Protocol

from scripts.events import catalogue, envelope, subjects
from scripts.events.bus import NoStream, PayloadTooLarge

ENTITIES = {"tasks": "task", "phases": "phase"}
INSERT = "INSERT INTO outbox VALUES (?, ?, ?, ?, ?)"
SLUGS = "SELECT DISTINCT slug FROM outbox WHERE id NOT IN (SELECT id FROM outbox_sent) ORDER BY slug"
PENDING = (
    "SELECT id, subject, value FROM outbox WHERE slug = ? AND id NOT IN (SELECT id FROM outbox_sent)"
    " ORDER BY revision, subject LIMIT ?"
)
SENT = "INSERT OR IGNORE INTO outbox_sent (id, sent_at) VALUES (?, ?)"
SUMMARY = "SELECT summary FROM ledgers WHERE slug = ?"
SENT_COLUMNS = "SELECT * FROM outbox_sent LIMIT 0"
ADD_SENT_AT = "ALTER TABLE outbox_sent ADD COLUMN sent_at INTEGER NOT NULL DEFAULT 0"
DUPLICATE = "duplicate column name: sent_at"
PRUNE_ROWS = "DELETE FROM outbox WHERE id IN (SELECT id FROM outbox_sent WHERE sent_at < ?)"
PRUNE_MARKS = "DELETE FROM outbox_sent WHERE sent_at < ?"
CAP_UNSENT = (
    "DELETE FROM outbox WHERE id NOT IN (SELECT id FROM outbox_sent) AND id NOT IN (SELECT id FROM outbox"
    " WHERE id NOT IN (SELECT id FROM outbox_sent) ORDER BY revision DESC, subject DESC, id DESC LIMIT ?)"
)
MAX_UNSENT = 10_000
BATCH = 100
AUTHOR = "ledger"
SMALL = "small"
RESERVED = re.compile(r"[%.*>\s]")


def token(key: str) -> str:
    return RESERVED.sub(lambda match: "".join(f"%{byte:02X}" for byte in match.group().encode()), key)


def changed(before: dict, after: dict) -> list:
    """(entity, id, item or None when removed) for every task and phase that differs."""
    found = []
    for collection, entity in ENTITIES.items():
        old = {item["id"]: item for item in before.get(collection, []) if "id" in item}
        new = {item["id"]: item for item in after.get(collection, []) if "id" in item}
        found += [
            (entity, key, new.get(key)) for key in sorted(old.keys() | new.keys()) if old.get(key) != new.get(key)
        ]
    return found


def write(connection: Any, slug: str, revision: int, before: dict, after: dict) -> None:
    """One relay row per changed task and phase, and none for a small ledger, which has no swarm stream to take them."""
    if (after or before).get("size") == SMALL:
        return
    for entity, key, item in changed(before, after):
        event = envelope.create(
            type=f"{entity}.{'removed' if item is None else 'changed'}",
            swarm=slug,
            entity=entity,
            key=key,
            version=revision,
            author=AUTHOR,
            data={} if item is None else catalogue.filled(entity, item),
        )
        connection.execute(
            INSERT, (event.id, slug, revision, subjects.subject(slug, entity, token(key)), event.encode().decode())
        )


class Publisher(Protocol):
    """What the relay needs from an event log: publishing one event under a subject."""

    def publish(self, subject: str, event: envelope.Envelope) -> object: ...


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def relay(repository: Any, events: Publisher, batch: int = BATCH, clock: Callable[[], int] = now_ms) -> int:
    """Publish up to a batch of each ledger's unsent rows under their row id, which the bus dedupes within its window.

    A row the bus refuses as too large is marked sent and reported, so it never holds back the rows after it.
    A swarm ledger whose events no stream takes keeps its rows unsent and never holds back another ledger's rows.
    """
    with repository.connect() as connection:
        return sum(
            relay_ledger(connection, slug, events, batch, clock) for (slug,) in connection.execute(SLUGS).fetchall()
        )


def relay_ledger(connection: Any, slug: str, events: Publisher, batch: int, clock: Callable[[], int]) -> int:
    """Publish up to a batch of one ledger's unsent rows in revision order, stopping at a row no stream takes.

    A row no stream takes is marked sent and reported instead when its ledger is small or no longer stored.
    """
    pending = connection.execute(PENDING, (slug, batch)).fetchall()
    for published, (row_id, subject, value) in enumerate(pending):
        try:
            events.publish(subject, slim(envelope.Envelope.decode(value.encode())))
        except PayloadTooLarge as error:
            sys.stderr.write(f"ledger outbox: row {row_id} skipped: {error}\n")
        except NoStream:
            if awaited(connection, slug):
                return published
            sys.stderr.write(f"ledger outbox: row {row_id} dropped: ledger {slug} is small or no longer stored\n")
        with connection:
            connection.execute(SENT, (row_id, clock()))
    return len(pending)


def awaited(connection: Any, slug: str) -> bool:
    """Whether a stream may yet take the ledger's rows: the ledger is still stored and its size is not small."""
    found = connection.execute(SUMMARY, (slug,)).fetchone()
    return found is not None and json.loads(found[0]).get("size") != SMALL


def slim(event: envelope.Envelope) -> envelope.Envelope:
    """The event with a changed entity's data cut to its catalogued fields, for rows written before the cut."""
    kind = catalogue.LEDGER.get(event.type)
    if kind is None or not kind.fields:
        return event
    return dataclasses.replace(event, data=catalogue.filled(event.entity, event.data))


def migrate(connection) -> None:
    if "sent_at" in {column[0] for column in connection.execute(SENT_COLUMNS).description}:
        return
    try:
        connection.execute(ADD_SENT_AT)
    except sqlite3.OperationalError as error:
        if DUPLICATE not in str(error):
            raise


def prune(repository, window_ms: int, clock: Callable[[], int] = now_ms) -> int:
    """Delete sent rows and their marks older than the bus dedupe window, which can no longer dedupe them."""
    cutoff = clock() - window_ms
    with repository.connect() as connection, connection:
        deleted = connection.execute(PRUNE_ROWS, (cutoff,)).rowcount
        connection.execute(PRUNE_MARKS, (cutoff,))
    return deleted


def cap(repository: Any, limit: int) -> int:
    """Delete the oldest unsent rows past the limit, so an outbox no bus drains stays bounded."""
    with repository.connect() as connection, connection:
        return connection.execute(CAP_UNSENT, (limit,)).rowcount
