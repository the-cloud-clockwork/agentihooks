import re

from scripts.events import envelope, subjects

ENTITIES = {"tasks": "task", "phases": "phase"}
INSERT = "INSERT INTO outbox VALUES (?, ?, ?, ?, ?)"
PENDING = (
    "SELECT id, subject, value FROM outbox WHERE id NOT IN (SELECT id FROM outbox_sent)"
    " ORDER BY revision, subject, id LIMIT ?"
)
SENT = "INSERT OR IGNORE INTO outbox_sent VALUES (?)"
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


def relay(repository, events, batch: int = BATCH) -> int:
    """Publish up to a batch of unsent rows; the row id is the event id, so the bus drops a republish after a crash."""
    with repository.connect() as connection:
        pending = connection.execute(PENDING, (batch,)).fetchall()
    for row_id, subject, value in pending:
        events.publish(subject, envelope.Envelope.decode(value.encode()))
        with repository.connect() as connection, connection:
            connection.execute(SENT, (row_id,))
    return len(pending)
