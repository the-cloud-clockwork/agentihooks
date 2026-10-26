import json

from scripts.swarm_ledger import models

FIELDS, RESOURCES, THREADS = TABLES = ("fields", "resources", "threads")
COLLECTIONS = models.RECORD_COLLECTIONS
THREAD_NAMES = models.THREAD_NAMES


ENCODER = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def encode(value: object) -> str:
    return ENCODER.encode(value)


def children(item, parts, table):
    """(child, parts, key, position, table) of a container, in the path scheme flatten stores."""
    if isinstance(item, dict):
        for index, (name, child) in enumerate(item.items()):
            bucket = THREADS if name in THREAD_NAMES else table
            if not parts and name in COLLECTIONS:
                bucket = RESOURCES
            yield child, [*parts, name], name, index, bucket
    elif isinstance(item, list):
        seen = {}
        for index, child in enumerate(item):
            identity = ["id", child["id"]] if isinstance(child, dict) and "id" in child else ["index", index]
            token = tuple(identity) if identity[0] == "index" or type(identity[1]) is str else encode(identity)
            occurrence = seen.get(token, 0)
            seen[token] = occurrence + 1
            yield child, [*parts, [*identity, occurrence]], index, index, table


def visit(rows, item, parts, parent, key, position, table):
    path = encode(parts)
    kind = "object" if isinstance(item, dict) else "array" if isinstance(item, list) else "value"
    rows[path] = (table, parent, encode(key), position, kind, encode(item) if kind == "value" else "null")
    for child, child_parts, name, index, bucket in children(item, parts, table):
        visit(rows, child, child_parts, path, name, index, bucket)


def flatten(value: object) -> dict:
    rows = {}
    visit(rows, value, [], None, None, 0, "fields")
    return rows


def _list_paths(children):
    paths = [child[1][-1] for child in children]
    return paths if all(path[0] == "index" or type(path[1]) is str for path in paths) else None


def _dict_paths_match(left, right):
    return (
        all(type(key) is str for key in left)
        and all(type(key) is str for key in right)
        and list(left) == list(right)[: len(left)]
    )


def _child_paths_match(left, right, old_children, new_children):
    if type(left) is list:
        old_paths, new_paths = _list_paths(old_children), _list_paths(new_children)
        if old_paths is not None and new_paths is not None:
            return old_paths == new_paths[: len(old_paths)]
    names = [encode(child[1]) for child in old_children]
    return names == [encode(child[1]) for child in new_children[: len(names)]]


def _walk_dictionary(left, right, parts, table, walk, after):
    encoded_parent = encode(parts)
    for index, (name, child) in enumerate(right.items()):
        if index < len(left) and left[name] is child and type(child) is not float:
            continue
        bucket = THREADS if name in THREAD_NAMES else table
        if not parts and name in COLLECTIONS:
            bucket = RESOURCES
        child_parts = [*parts, name]
        if index < len(left):
            walk(left[name], child, child_parts, encoded_parent, name, index, bucket)
        else:
            visit(after, child, child_parts, encoded_parent, name, index, bucket)


def diff(old: object, new: object) -> tuple[dict, dict]:
    """The stored rows of `old` and `new` under every path that differs, descending only into changed values."""
    before, after = {}, {}

    def walk(left, right, parts, parent, key, position, table):
        if type(left) is type(right) and left == right:
            return
        same_shape = type(left) is type(right) and isinstance(left, (dict, list))
        if same_shape:
            if type(left) is dict and _dict_paths_match(left, right):
                _walk_dictionary(left, right, parts, table, walk, after)
                return
            old_children = list(children(left, parts, table))
            new_children = list(children(right, parts, table))
            if _child_paths_match(left, right, old_children, new_children):
                encoded_parent = encode(parts)
                for (old_child, child_parts, name, index, bucket), new_child in zip(old_children, new_children):
                    walk(old_child, new_child[0], child_parts, encoded_parent, name, index, bucket)
                for child, child_parts, name, index, bucket in new_children[len(old_children) :]:
                    visit(after, child, child_parts, encoded_parent, name, index, bucket)
                return
        visit(before, left, parts, parent, key, position, table)
        visit(after, right, parts, parent, key, position, table)

    walk(old, new, [], None, None, 0, "fields")
    return before, after


def assemble(rows: dict) -> object:
    children = {}
    for path, row in rows.items():
        children.setdefault(row[1], []).append((path, row))

    def build(path):
        row = rows[path]
        if row[4] == "value":
            return json.loads(row[5])
        ordered = sorted(children.get(path, []), key=lambda child: child[1][3])
        if row[4] == "array":
            return [build(key) for key, _ in ordered]
        return {json.loads(child[2]): build(key) for key, child in ordered}

    try:
        return build("[]")
    finally:
        build = None


def read_rows(connection, slug: str) -> dict:
    rows = {}
    for table in TABLES:
        for path, parent, key, position, kind, value in connection.execute(
            f"SELECT path, parent, key, position, kind, value FROM {table} WHERE slug=?", (slug,)
        ):
            rows[path] = (table, parent, key, position, kind, value)
    return rows


def load(connection, slug: str) -> object:
    """assemble(read_rows(connection, slug)) without first collecting every stored row and its raw value text."""
    read = getattr(connection, "stream", connection.execute)
    containers = {}
    for table in TABLES:
        for path, kind in read(f"SELECT path, kind FROM {table} WHERE slug=? AND kind<>'value'", (slug,)):
            containers[path] = [] if kind == "array" else {}
    placed = {}
    for table in TABLES:
        for path, parent, key, position, kind, value in read(
            f"SELECT path, parent, key, position, kind, value FROM {table} WHERE slug=?", (slug,)
        ):
            child = json.loads(value) if kind == "value" else containers[path]
            placed.setdefault(parent, []).append((position, key, child))
    for parent, items in placed.items():
        container = containers.get(parent)
        items.sort()
        if type(container) is list:
            container.extend(child for _, _, child in items)
        elif container is not None:
            container.update((json.loads(key), child) for _, key, child in items)
    return containers["[]"]


def changes(before: dict, after: dict) -> dict:
    missing = object()
    return {
        key: after.get(key)
        for key in before.keys() | after.keys()
        if before.get(key, missing) != after.get(key, missing)
    }


def write_rows(connection, slug: str, before: dict, after: dict) -> list:
    deletes, upserts = {}, {}
    changed = changes(before, after)
    for path, row in changed.items():
        old = before.get(path)
        if old and (row is None or old[0] != row[0]):
            deletes.setdefault(old[0], []).append((slug, path))
        if row is not None:
            upserts.setdefault(row[0], []).append((slug, path, *row[1:]))
    for table, rows in deletes.items():
        connection.executemany(f"DELETE FROM {table} WHERE slug=? AND path=?", rows)
    for table, rows in upserts.items():
        connection.executemany(
            f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(slug,path) DO UPDATE SET parent=excluded.parent,key=excluded.key,position=excluded.position,kind=excluded.kind,value=excluded.value",
            rows,
        )
    return list(changed)
