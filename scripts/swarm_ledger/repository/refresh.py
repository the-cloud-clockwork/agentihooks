import json

from .rows import TABLES, children, encode

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS row_changes (slug TEXT, generation INTEGER, paths TEXT, PRIMARY KEY(slug,generation));\n"
    + "".join(f"CREATE INDEX IF NOT EXISTS {table}_parent ON {table}(slug,parent);\n" for table in TABLES)
)
RECORD = "INSERT INTO row_changes VALUES (?, ?, ?)"
PRUNE = "DELETE FROM row_changes WHERE slug=? AND generation<=?"
CHANGES = (
    "SELECT generation, paths FROM row_changes WHERE slug=? AND generation>? AND generation<=? ORDER BY generation"
)
ROOT = "[]"


def record(connection, slug: str, generation: int, paths, kept: int) -> None:
    connection.execute(RECORD, (slug, generation, encode(sorted(paths))))
    connection.execute(PRUNE, (slug, generation - kept))


def changed_paths(connection, slug: str, since: int, generation: int, kept: int) -> set | None:
    if generation - since > kept:
        return None
    rows = connection.execute(CHANGES, (slug, since, generation)).fetchall()
    if [row[0] for row in rows] != list(range(since + 1, generation + 1)):
        return None
    return {path for _, paths in rows for path in json.loads(paths)}


def touched(paths: set) -> set:
    above = set(paths)
    for path in paths:
        parts = json.loads(path)
        above.update(encode(parts[:depth]) for depth in range(len(parts)))
    return above


def child_rows(connection, slug: str, parent: str) -> list:
    rows = []
    for table in TABLES:
        rows.extend(
            connection.execute(
                f"SELECT path, key, position, kind, value FROM {table} WHERE slug=? AND parent=?", (slug, parent)
            )
        )
    return sorted(rows, key=lambda row: row[2])


def build(connection, slug: str, path: str, kind: str, old: object, dirty: set) -> object:
    """The container at path as stored now, reusing each child of its old value that no change reached."""
    known = {encode(parts): child for child, parts, *_ in children(old, json.loads(path), None)}
    items = []
    for child, key, _, child_kind, value in child_rows(connection, slug, path):
        if child_kind == "value":
            item = json.loads(value)
        elif child in dirty:
            item = build(connection, slug, child, child_kind, known.get(child), dirty)
        else:
            item = known[child]
        items.append((json.loads(key), item))
    return [item for _, item in items] if kind == "array" else dict(items)


def refreshed(connection, slug: str, state: dict, paths: set) -> dict | None:
    try:
        return build(connection, slug, ROOT, "object", state, touched(paths))
    except KeyError:
        return None
