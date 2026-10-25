import json
from contextlib import closing, contextmanager
from pathlib import Path


def _path(path: Path) -> Path:
    return path.with_suffix(".scheduler.sqlite3")


@contextmanager
def _connection(path: Path):
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(_path(path))) as database, database:
        database.execute("CREATE TABLE IF NOT EXISTS emissions (id TEXT PRIMARY KEY, entry TEXT)")
        database.execute("CREATE INDEX IF NOT EXISTS active_emissions ON emissions(id) WHERE entry IS NOT NULL")
        yield database


def publish(path: Path, entry: dict) -> None:
    with _connection(path) as database:
        database.execute("INSERT OR IGNORE INTO emissions VALUES (?, ?)", (entry["id"], json.dumps(entry)))


def merge(path: Path, messages: list[dict]) -> list[dict]:
    if not _path(path).exists():
        return messages
    with _connection(path) as database:
        ordinary = [
            message
            for message in messages
            if not database.execute("SELECT 1 FROM emissions WHERE id = ?", (message["id"],)).fetchone()
        ]
        scheduled = [
            json.loads(row[0])
            for row in database.execute("SELECT entry FROM emissions WHERE entry IS NOT NULL ORDER BY rowid")
        ]
    return ordinary + scheduled


def save(path: Path, messages: list[dict]) -> list[dict]:
    if not _path(path).exists():
        return messages
    updates = {message["id"]: message for message in messages}
    with _connection(path) as database:
        for entry_id, raw in database.execute("SELECT id, entry FROM emissions WHERE entry IS NOT NULL").fetchall():
            updated = json.dumps(updates[entry_id]) if entry_id in updates else None
            if updated != raw:
                database.execute("UPDATE emissions SET entry = ? WHERE id = ?", (updated, entry_id))
        return [
            message
            for message in messages
            if not database.execute("SELECT 1 FROM emissions WHERE id = ?", (message["id"],)).fetchone()
        ]
