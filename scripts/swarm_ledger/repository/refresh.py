import json
from sqlite3 import Connection
from typing import Any, cast

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
PATHS_KEPT = 5000


def record(connection: Connection, slug: str, generation: int, paths: list, kept: int) -> None:
    stored = encode(sorted(paths)) if len(paths) <= PATHS_KEPT else "null"
    connection.execute(RECORD, (slug, generation, stored))
    connection.execute(PRUNE, (slug, generation - kept))


def changed_paths(connection: Connection, slug: str, since: int, generation: int, kept: int) -> set | None:
    if generation - since > kept:
        return None
    rows = connection.execute(CHANGES, (slug, since, generation)).fetchall()
    if [row[0] for row in rows] != list(range(since + 1, generation + 1)):
        return None
    recorded = [json.loads(paths) for _, paths in rows]
    if None in recorded:
        return None
    return {path for paths in recorded for path in paths}


def touched(paths: set) -> set:
    above = set(paths)
    for path in paths:
        parts = json.loads(path)
        above.update(encode(parts[:depth]) for depth in range(len(parts)))
    return above


def child_rows(connection: Connection, slug: str, parent: str) -> list:
    rows = []
    for table in TABLES:
        rows.extend(
            connection.execute(
                f"SELECT path, key, position, kind, value FROM {table} WHERE slug=? AND parent=?", (slug, parent)
            )
        )
    return sorted(rows, key=lambda row: row[2])


def build(
    connection: Connection, slug: str, path: str, old: Any, dirty: set[str], changed: set[str] | None = None
) -> list[tuple[Any, Any]]:
    """The children of the container at path as stored now, reusing each child of its old value that no change
    reached; given the changed paths, an object child rereads only the keys they reach."""
    known: dict[str, Any] = {
        encode(parts): child for child, parts, *_ in children(old, json.loads(path), cast(str, None))
    }
    items: list[tuple[Any, Any]] = []
    rows: list[tuple[Any, ...]] = child_rows(connection, slug, path)
    for child, key, _, child_kind, value in rows:
        item: Any = None
        if child_kind == "value":
            item = json.loads(value)
        elif child in dirty:
            held = known.get(child)
            if changed is not None and child_kind == "object" and isinstance(held, dict):
                item = patched(connection, slug, child, cast(dict[str, Any], held), beneath(changed, child))
            if item is None:
                pairs = build(connection, slug, child, held, dirty, changed)
                item = [entry for _, entry in pairs] if child_kind == "array" else dict(pairs)
        else:
            item = known[child]
        items.append((json.loads(key), item))
    return items


def patched(
    connection: Connection, slug: str, path: str, old: dict[str, Any], paths: set[str]
) -> dict[str, Any] | None:
    """The object at path as stored now, rereading only the keys paths reach in their stored order; None when the
    object itself changed or a reached key is gone, so the caller rereads every key."""
    if path in paths:
        return None
    depth, dirty, item = len(json.loads(path)), touched(paths), dict(old)
    reached: list[tuple[str, Any]] = [
        (child, stored_row(connection, slug, child))
        for child in {encode(json.loads(changed)[: depth + 1]) for changed in paths}
    ]
    if any(row is None for _, row in reached):
        return None
    for child, row in sorted(reached, key=lambda pair: pair[1][0]):
        key: str = json.loads(child)[-1]
        held: Any = old.get(key)
        under = beneath(paths, child)
        nested = (
            patched(connection, slug, child, held, under) if row[1] == "object" and isinstance(held, dict) else None
        )
        if row[1] == "value":
            item[key] = json.loads(row[2])
        elif nested is not None:
            item[key] = nested
        else:
            pairs = build(connection, slug, child, held, dirty, paths)
            item[key] = [entry for _, entry in pairs] if row[1] == "array" else dict(pairs)
    return item


def beneath(paths: set[str], path: str) -> set[str]:
    """The paths at or under the encoded path."""
    return {changed for changed in paths if changed == path or changed.startswith(path[:-1] + ",")}


def refreshed(connection: Connection, slug: str, state: dict, paths: set) -> dict | None:
    try:
        return dict(build(connection, slug, ROOT, state, touched(paths), paths))
    except KeyError:
        return None


def stored_row(connection: Connection, slug: str, path: str) -> tuple | None:
    for table in TABLES:
        row = connection.execute(f"SELECT position, kind, value FROM {table} WHERE slug=? AND path=?", (slug, path))
        found = row.fetchone()
        if found is not None:
            return found
    return None


def rebuilt(connection: Connection, slug: str, path: str, old: list, paths: set) -> list | None:
    """The array at path as stored now, rebuilding only the children paths reached; None when the array itself
    changed or a child was added, removed or moved."""
    if path in paths:
        return None
    parts, dirty, items = json.loads(path), touched(paths), list(old)
    for child in {encode(json.loads(changed)[: len(parts) + 1]) for changed in paths}:
        row = stored_row(connection, slug, child)
        if row is None or row[1] != "object" or not same_child(old, row[0], json.loads(child)[-1]):
            return None
        items[row[0]] = dict(build(connection, slug, child, old[row[0]], dirty))
    return items


def same_child(old: list, position: int, identity: list) -> bool:
    """Whether the child stored at position is the first old child with the id identity names."""
    if identity[0] != "id" or identity[2] != 0 or position >= len(old):
        return False
    item = old[position]
    return isinstance(item, dict) and item.get("id") == identity[1]
