"""The scoped learned listing a seat reader takes: its own notes plus canon and insight shared by other seats."""

import pytest

from scripts.inbox.seats import SeatMemory

pytestmark = pytest.mark.unit


@pytest.mark.feature("seat-brief")
def test_a_seat_reads_its_own_notes_and_the_canon_and_insight_of_other_seats() -> None:
    import fakeredis

    memory = SeatMemory(fakeredis.FakeRedis(decode_responses=True))
    for seat, text, maturity in (
        ("eng-1@sw", "own note", "note"),
        ("eng-1@sw", "own data", "data"),
        ("ci-1@sw", "other note", "note"),
        ("ci-1@sw", "shared canon", "canon"),
        ("ci-1@sw", "shared insight", "insight"),
        ("eng-1@elsewhere", "other swarm canon", "canon"),
    ):
        memory.learn(seat, "earlier occupant", text, 1, maturity)

    texts = {note["text"] for note in memory.scoped("sw", "eng-1@sw")}

    assert texts == {"own note", "own data", "shared canon", "shared insight"}
