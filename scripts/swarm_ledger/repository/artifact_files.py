import base64

SAVE = "INSERT OR IGNORE INTO artifact_files VALUES (?, ?, ?)"
LOAD = "SELECT data FROM artifact_files WHERE slug=? AND id=?"
DROP = "DELETE FROM artifact_files WHERE slug=? AND id=?"


def _repository():
    from scripts.swarm_ledger import storage

    return storage.default()


def save(slug: str, file_id: str, data: bytes) -> None:
    with _repository().connect() as connection, connection:
        connection.execute(SAVE, (slug, file_id, base64.b64encode(data).decode("ascii")))


def load(slug: str, file_id: str) -> bytes | None:
    with _repository().connect() as connection:
        row = connection.execute(LOAD, (slug, file_id)).fetchone()
    return None if row is None else base64.b64decode(row[0])


def drop(slug: str, file_ids: set[str]) -> None:
    if not file_ids:
        return
    with _repository().connect() as connection, connection:
        for file_id in sorted(file_ids):
            connection.execute(DROP, (slug, file_id))
