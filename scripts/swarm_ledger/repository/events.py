import hashlib
import json

from .rows import changes, encode

LAST = "SELECT MAX(position) FROM events WHERE slug=?"
COUNT = "SELECT COUNT(*) FROM events WHERE slug=?"
APPEND = "INSERT INTO events VALUES (?, ?, ?, ?, ?)"
TRIM = (
    "DELETE FROM events WHERE slug=? AND position NOT IN "
    "(SELECT position FROM events WHERE slug=? ORDER BY position DESC LIMIT ?)"
)


def read_events(connection, slug: str, revision: int | None = None) -> list:
    query = "SELECT value FROM events WHERE slug=?"
    parameters = (slug,)
    if revision is not None:
        if connection.execute("SELECT 1 FROM events WHERE slug=? AND revision IS NULL LIMIT 1", (slug,)).fetchone():
            raise KeyError("rev")
        query += " AND revision>?"
        parameters += (revision,)
    return [json.loads(value) for (value,) in connection.execute(query + " ORDER BY position", parameters)]


def write_events(connection, slug: str, events: list) -> None:
    desired = {}
    occurrences = {}
    for position, event in enumerate(events):
        value = encode(event)
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        occurrence = occurrences.get(digest, 0)
        occurrences[digest] = occurrence + 1
        desired[f"{digest}:{occurrence}"] = (event.get("rev"), position, value)
    old = {
        key: (revision, position, value)
        for key, revision, position, value in connection.execute(
            "SELECT key,revision,position,value FROM events WHERE slug=?", (slug,)
        )
    }
    for key, row in changes(old, desired).items():
        if row is None:
            connection.execute("DELETE FROM events WHERE slug=? AND key=?", (slug, key))
        elif key in old and old[key][2] == row[2]:
            connection.execute("UPDATE events SET position=? WHERE slug=? AND key=?", (row[1], slug, key))
        else:
            connection.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                (slug, key, *row),
            )


def append_events(connection, slug: str, events: list, kept: int) -> None:
    """Add a mutation's events after the newest one and drop the oldest past `kept`; older rows stay untouched."""
    if not events:
        return
    (last,) = connection.execute(LAST, (slug,)).fetchone()
    start = -1 if last is None else last
    for offset, event in enumerate(events, 1):
        connection.execute(APPEND, (slug, f"p{start + offset}", event.get("rev"), start + offset, encode(event)))
    connection.execute(TRIM, (slug, slug, kept))


def trim_events(connection, slug: str, kept: int) -> None:
    if connection.execute(COUNT, (slug,)).fetchone()[0] > kept:
        connection.execute(TRIM, (slug, slug, kept))


def retained(events: list, ack: int | None, kept: int, ceiling: int) -> int:
    """Index of the oldest event to keep: the newest `kept`, every one after `ack`, never more than `ceiling`."""
    start = max(0, len(events) - kept)
    if ack is not None:
        while start and events[start - 1].get("rev", 0) > ack:
            start -= 1
    return max(start, len(events) - ceiling)
