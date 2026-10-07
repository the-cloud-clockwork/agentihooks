import json

from .rows import assemble, changes, encode, flatten


def read_seeds(connection, slug: str) -> dict:
    rows = {
        path: tuple(json.loads(value))
        for path, value in connection.execute("SELECT path,value FROM seed_base WHERE slug=?", (slug,))
    }
    seeds = {}
    for (revision,) in connection.execute("SELECT revision FROM revisions WHERE slug=? ORDER BY position", (slug,)):
        for path, value in connection.execute(
            "SELECT path,value FROM seed_deltas WHERE slug=? AND revision=?", (slug, revision)
        ):
            if value is None:
                rows.pop(path, None)
            else:
                rows[path] = tuple(json.loads(value))
        seeds[revision] = assemble(rows)
    return seeds


def sync_values(connection, table: str, slug: str, values: dict) -> None:
    before = dict(connection.execute(f"SELECT path,value FROM {table} WHERE slug=?", (slug,)))
    for path, value in changes(before, values).items():
        if path not in values:
            connection.execute(f"DELETE FROM {table} WHERE slug=? AND path=?", (slug, path))
        else:
            connection.execute(
                f"INSERT INTO {table} VALUES (?, ?, ?) ON CONFLICT(slug,path) DO UPDATE SET value=excluded.value",
                (slug, path, value),
            )


def write_seeds(connection, slug: str, seeds: dict) -> None:
    snapshots = [(str(revision), flatten(seed)) for revision, seed in seeds.items()]
    baseline = snapshots[0][1] if snapshots else {}
    sync_values(connection, "seed_base", slug, {path: encode(row) for path, row in baseline.items()})
    desired = {}
    previous = baseline
    revisions = {}
    for index, (revision, rows) in enumerate(snapshots):
        revisions[revision] = index
        desired.update(
            {
                (revision, path): encode(row) if row is not None else None
                for path, row in changes(previous, rows).items()
            }
        )
        previous = rows
    old = {
        (revision, path): value
        for revision, path, value in connection.execute(
            "SELECT revision,path,value FROM seed_deltas WHERE slug=?", (slug,)
        )
    }
    for (revision, path), value in changes(old, desired).items():
        if (revision, path) not in desired:
            connection.execute("DELETE FROM seed_deltas WHERE slug=? AND revision=? AND path=?", (slug, revision, path))
        else:
            connection.execute(
                "INSERT INTO seed_deltas VALUES (?, ?, ?, ?) ON CONFLICT(slug,revision,path) DO UPDATE SET value=excluded.value",
                (slug, revision, path, value),
            )
    old_revisions = dict(connection.execute("SELECT revision,position FROM revisions WHERE slug=?", (slug,)))
    for revision in old_revisions.keys() - revisions.keys():
        connection.execute("DELETE FROM revisions WHERE slug=? AND revision=?", (slug, revision))
    for revision, position in revisions.items():
        if old_revisions.get(revision) != position:
            connection.execute(
                "INSERT INTO revisions VALUES (?, ?, ?) ON CONFLICT(slug,revision) DO UPDATE SET position=excluded.position",
                (slug, revision, position),
            )
