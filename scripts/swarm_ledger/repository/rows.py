import json

FIELDS, RESOURCES, THREADS = TABLES = ("fields", "resources", "threads")
COLLECTIONS = frozenset(
    (
        "phases",
        "tasks",
        "questions",
        "followups",
        "notes",
        "chat",
        "artifacts",
        "artifact_trash",
        "priorities",
        "notifications",
        "alerts",
        "plans",
        "slices",
    )
)


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def children(item, parts, table):
    """(child, parts, key, position, table) of a container, in the path scheme flatten stores."""
    if isinstance(item, dict):
        for index, (name, child) in enumerate(item.items()):
            bucket = THREADS if name in ("comments", "answers") else table
            if not parts and name in COLLECTIONS:
                bucket = RESOURCES
            yield child, [*parts, name], name, index, bucket
    elif isinstance(item, list):
        seen = {}
        for index, child in enumerate(item):
            identity = ["id", child["id"]] if isinstance(child, dict) and "id" in child else ["index", index]
            token = encode(identity)
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


def diff(old: object, new: object) -> tuple[dict, dict]:
    """The stored rows of `old` and `new` under every path that differs, descending only into changed values."""
    before, after = {}, {}

    def walk(left, right, parts, parent, key, position, table):
        if type(left) is type(right) and left == right:
            return
        same_shape = type(left) is type(right) and isinstance(left, (dict, list))
        if same_shape:
            old_children = list(children(left, parts, table))
            new_children = list(children(right, parts, table))
            names = [encode(child[1]) for child in old_children]
            if names == [encode(child[1]) for child in new_children[: len(names)]]:
                for (old_child, child_parts, name, index, bucket), new_child in zip(old_children, new_children):
                    walk(old_child, new_child[0], child_parts, encode(parts), name, index, bucket)
                for child, child_parts, name, index, bucket in new_children[len(names) :]:
                    visit(after, child, child_parts, encode(parts), name, index, bucket)
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

    return build("[]")


def read_rows(connection, slug: str) -> dict:
    rows = {}
    for table in TABLES:
        for path, parent, key, position, kind, value in connection.execute(
            f"SELECT path, parent, key, position, kind, value FROM {table} WHERE slug=?", (slug,)
        ):
            rows[path] = (table, parent, key, position, kind, value)
    return rows


def changes(before: dict, after: dict) -> dict:
    missing = object()
    return {
        key: after.get(key)
        for key in before.keys() | after.keys()
        if before.get(key, missing) != after.get(key, missing)
    }


def write_rows(connection, slug: str, before: dict, after: dict) -> None:
    for path, row in changes(before, after).items():
        old = before.get(path)
        if old and (row is None or old[0] != row[0]):
            connection.execute(f"DELETE FROM {old[0]} WHERE slug=? AND path=?", (slug, path))
        if row is not None:
            connection.execute(
                f"INSERT INTO {row[0]} VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(slug,path) DO UPDATE SET parent=excluded.parent,key=excluded.key,position=excluded.position,kind=excluded.kind,value=excluded.value",
                (slug, path, *row[1:]),
            )
