import json

TABLES = ("fields", "resources", "threads")
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
    )
)


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def flatten(value: object) -> dict:
    rows = {}

    def visit(item, parts, parent, key, position, table):
        path = encode(parts)
        kind = "object" if isinstance(item, dict) else "array" if isinstance(item, list) else "value"
        rows[path] = (table, parent, encode(key), position, kind, encode(item) if kind == "value" else "null")
        if isinstance(item, dict):
            for index, (name, child) in enumerate(item.items()):
                bucket = "threads" if name in ("comments", "answers") else table
                if not parts and name in COLLECTIONS:
                    bucket = "resources"
                visit(child, [*parts, name], path, name, index, bucket)
        elif isinstance(item, list):
            seen = {}
            for index, child in enumerate(item):
                identity = ["id", child["id"]] if isinstance(child, dict) and "id" in child else ["index", index]
                token = encode(identity)
                occurrence = seen.get(token, 0)
                seen[token] = occurrence + 1
                visit(child, [*parts, [*identity, occurrence]], path, index, index, table)

    visit(value, [], None, None, 0, "fields")
    return rows


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
