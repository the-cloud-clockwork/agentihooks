import base64

TABLE = "media_files"

SAVE = "INSERT INTO media_files VALUES (?, ?, ?, ?) ON CONFLICT(slug,id) DO UPDATE SET saved_at=excluded.saved_at"
LOAD = "SELECT data FROM media_files WHERE slug=? AND id=?"
AGES = "SELECT id, saved_at FROM media_files WHERE slug=?"
DROP = "DELETE FROM media_files WHERE slug=? AND id=?"


def _repository():
    from scripts.swarm_ledger import storage

    return storage.default()


def save(slug: str, media_id: str, data: bytes, at: int) -> None:
    with _repository().connect() as connection, connection:
        connection.execute(SAVE, (slug, media_id, base64.b64encode(data).decode(), at))


def load(slug: str, media_id: str) -> bytes | None:
    with _repository().connect() as connection:
        row = connection.execute(LOAD, (slug, media_id)).fetchone()
    return None if row is None else base64.b64decode(row[0])


def ages(slug: str) -> dict[str, int]:
    with _repository().connect() as connection:
        return dict(connection.execute(AGES, (slug,)).fetchall())


def drop(slug: str, media_ids: set[str]) -> None:
    if not media_ids:
        return
    with _repository().connect() as connection, connection:
        for media_id in sorted(media_ids):
            connection.execute(DROP, (slug, media_id))
