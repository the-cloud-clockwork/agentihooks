from scripts.events import envelope, subjects

ENTITIES = {"tasks": "task", "phases": "phase"}
INSERT = "INSERT INTO outbox VALUES (?, ?, ?, ?, ?)"
AUTHOR = "ledger"


def changed(before: dict, after: dict) -> list:
    """(entity, id, item or None when removed) for every task and phase that differs."""
    found = []
    for collection, entity in ENTITIES.items():
        old = {item["id"]: item for item in before.get(collection, [])}
        new = {item["id"]: item for item in after.get(collection, [])}
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
            INSERT, (event.id, slug, revision, subjects.subject(slug, entity, key), event.encode().decode())
        )
