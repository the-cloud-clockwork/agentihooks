import re
import time
from collections.abc import Callable

from scripts.events import envelope, subjects

ENTITIES = {"tasks": "task", "phases": "phase"}
INSERT = "INSERT INTO outbox VALUES (?, ?, ?, ?, ?)"
PENDING = (
    "SELECT id, subject, value FROM outbox WHERE id NOT IN (SELECT id FROM outbox_sent)"
    " ORDER BY revision, subject LIMIT ?"
)
SENT = "INSERT OR IGNORE INTO outbox_sent (id, sent_at) VALUES (?, ?)"
SENT_COLUMNS = "SELECT * FROM outbox_sent LIMIT 0"
ADD_SENT_AT = "ALTER TABLE outbox_sent ADD COLUMN sent_at INTEGER NOT NULL DEFAULT 0"
PRUNE_ROWS = "DELETE FROM outbox WHERE id IN (SELECT id FROM outbox_sent WHERE sent_at < ?)"
PRUNE_MARKS = "DELETE FROM outbox_sent WHERE sent_at < ?"
BATCH = 100
AUTHOR = "ledger"
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


def write(connection, slug: str, revision: int, before: dict, after: dict) -> None:
    for entity, key, item in changed(before, after):
        event = envelope.create(
            type=f"{entity}.{'removed' if item is None else 'changed'}",
            swarm=slug,
            entity=entity,
            key=key,
            version=revision,
            author=AUTHOR,
            data=item or {},
        )
        connection.execute(
            INSERT, (event.id, slug, revision, subjects.subject(slug, entity, token(key)), event.encode().decode())
        )


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def relay(repository, events, batch: int = BATCH, clock: Callable[[], int] = now_ms) -> int:
    """Publish up to a batch of unsent rows under their row id, which the bus dedupes within its window."""
    with repository.connect() as connection:
        pending = connection.execute(PENDING, (batch,)).fetchall()
        for row_id, subject, value in pending:
            events.publish(subject, envelope.Envelope.decode(value.encode()))
            with connection:
                connection.execute(SENT, (row_id, clock()))
    return len(pending)


def migrate(connection) -> None:
    if "sent_at" not in {column[0] for column in connection.execute(SENT_COLUMNS).description}:
        connection.execute(ADD_SENT_AT)


def prune(repository, window_ms: int, clock: Callable[[], int] = now_ms) -> int:
    """Delete sent rows and their marks older than the bus dedupe window, which can no longer dedupe them."""
    cutoff = clock() - window_ms
    with repository.connect() as connection, connection:
        deleted = connection.execute(PRUNE_ROWS, (cutoff,)).rowcount
        connection.execute(PRUNE_MARKS, (cutoff,))
    return deleted
