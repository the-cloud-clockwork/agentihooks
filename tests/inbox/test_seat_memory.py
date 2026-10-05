import pytest

from scripts.inbox.seats import SeatMemory

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def memory():
    import fakeredis

    return SeatMemory(fakeredis.FakeRedis(decode_responses=True))


def test_a_seat_with_no_history_has_no_recaps_and_no_learned_notes(memory):
    assert memory.recaps("eng-1@rig") == []
    assert memory.learned("eng-1@rig") == []


def test_older_recaps_survive_a_new_one_newest_first(memory):
    memory.add_recap("eng-1@rig", "rig-eng-1", "t1", "did seam 1", at=10)
    memory.add_recap("eng-1@rig", "rig-eng-4", "t1", "did seam 2", at=20)
    assert memory.recaps("eng-1@rig") == [
        {"occupant": "rig-eng-4", "task": "t1", "text": "did seam 2", "at": 20},
        {"occupant": "rig-eng-1", "task": "t1", "text": "did seam 1", "at": 10},
    ]


def test_learned_notes_accumulate_in_order_per_seat(memory):
    memory.learn("eng-1@rig", "rig-eng-1", "run ruff format --check too", at=10)
    memory.learn("eng-1@rig", "rig-eng-4", "fakeredis needs the xdist group", at=20)
    memory.learn("eng-2@rig", "rig-eng-2", "other seat", at=30)
    assert [n["text"] for n in memory.learned("eng-1@rig")] == [
        "run ruff format --check too",
        "fakeredis needs the xdist group",
    ]
    assert memory.learned("eng-1@rig")[0]["occupant"] == "rig-eng-1"
