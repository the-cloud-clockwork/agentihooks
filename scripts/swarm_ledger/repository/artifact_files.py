import base64

TABLE = "artifact_files"

SAVE = "INSERT OR IGNORE INTO artifact_files VALUES (?, ?, ?)"
LOAD = "SELECT data FROM artifact_files WHERE slug=? AND id=?"
LIST = "SELECT id, 0 FROM artifact_files WHERE slug=?"
DROP = "DELETE FROM artifact_files WHERE slug=? AND id=?"


def _repository():
    from scripts.swarm_ledger import storage

    return storage.default()


def save(slug: str, file_id: str, data: bytes) -> None:
    repository = _repository()
    encoded = base64.b64encode(data).decode()
    if repository.remote:
        repository.file_save(TABLE, slug, file_id, encoded)
        return
    with repository.connect() as connection, connection:
        connection.execute(SAVE, (slug, file_id, encoded))


def load(slug: str, file_id: str) -> bytes | None:
    repository = _repository()
    if repository.remote:
        value = repository.file_load(TABLE, slug, file_id)
        return None if value is None else base64.b64decode(value)
    with repository.connect() as connection:
        row = connection.execute(LOAD, (slug, file_id)).fetchone()
    return None if row is None else base64.b64decode(row[0])


def ids(slug: str) -> set[str]:
    repository = _repository()
    if repository.remote:
        return set(repository.file_list(TABLE, slug))
    with repository.connect() as connection:
        return {row[0] for row in connection.execute(LIST, (slug,)).fetchall()}


def drop(slug: str, file_ids: set[str]) -> None:
    if not file_ids:
        return
    repository = _repository()
    if repository.remote:
        repository.file_drop(TABLE, slug, file_ids)
        return
    with repository.connect() as connection, connection:
        for file_id in sorted(file_ids):
            connection.execute(DROP, (slug, file_id))
