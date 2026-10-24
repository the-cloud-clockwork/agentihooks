import sqlite3

import pytest

from scripts.swarm_ledger.repository.events import append_events
from scripts.swarm_ledger.repository.rows import encode
from scripts.swarm_ledger.repository.sqlite import SCHEMA


@pytest.fixture
def connection():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    yield conn
    conn.close()


def rows(connection, slug):
    return connection.execute(
        "SELECT key, revision, position, value FROM events WHERE slug=? ORDER BY position", (slug,)
    ).fetchall()


def test_no_events_leaves_existing_rows_untouched_even_past_kept(connection):
    append_events(connection, "s", [{"rev": 1, "op": "a"}, {"rev": 2, "op": "b"}], kept=10)
    before = rows(connection, "s")
    append_events(connection, "s", [], kept=1)
    assert rows(connection, "s") == before


def test_the_first_event_lands_at_position_zero_with_its_revision_and_encoded_value(connection):
    event = {"rev": 7, "op": "add", "text": "hello"}
    append_events(connection, "s", [event], kept=10)
    assert rows(connection, "s") == [("p0", 7, 0, encode(event))]


def test_a_second_append_continues_after_the_newest_position_and_prunes_to_kept(connection):
    append_events(connection, "s", [{"rev": 1, "op": "a"}, {"rev": 2, "op": "b"}], kept=10)
    append_events(connection, "s", [{"rev": 3, "op": "c"}], kept=2)
    assert rows(connection, "s") == [
        ("p1", 2, 1, encode({"rev": 2, "op": "b"})),
        ("p2", 3, 2, encode({"rev": 3, "op": "c"})),
    ]
