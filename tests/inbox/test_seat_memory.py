import json

import pytest

from scripts.inbox.seats import SeatError, SeatMemory, SwarmCulture

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


def test_a_learned_note_is_a_note_unless_told_otherwise(memory):
    memory.learn("eng-1@rig", "rig-eng-1", "plain lesson", at=10)
    memory.learn("eng-1@rig", "rig-eng-1", "raw figure", at=11, maturity="data")
    assert [n["maturity"] for n in memory.learned("eng-1@rig")] == ["note", "data"]


def test_an_entry_written_before_maturity_reads_as_a_note(memory):
    memory.redis.rpush(memory.key("eng-1@rig", "learned"), json.dumps({"occupant": "a", "text": "old", "at": 1}))
    assert memory.learned("eng-1@rig")[0]["maturity"] == "note"


def test_learn_refuses_an_unknown_maturity(memory):
    with pytest.raises(SeatError):
        memory.learn("eng-1@rig", "rig-eng-1", "x", at=1, maturity="gospel")


def test_promote_raises_an_entry_and_keeps_the_reason(memory):
    memory.learn("eng-1@rig", "rig-eng-1", "first", at=1)
    memory.learn("eng-1@rig", "rig-eng-1", "second", at=2)
    memory.promote("eng-1@rig", 2, "insight", "rig-eng-3", "held on three tasks", at=5)
    first, second = memory.learned("eng-1@rig")
    assert (first["maturity"], second["maturity"]) == ("note", "insight")
    assert second["promotions"] == [
        {"from": "note", "to": "insight", "by": "rig-eng-3", "reason": "held on three tasks", "at": 5}
    ]


@pytest.mark.parametrize(
    ("number", "maturity", "reason"),
    [
        (1, "note", "same level"),
        (1, "data", "lower"),
        (1, "insight", "  "),
        (2, "insight", "no entry 2"),
        (0, "canon", "x"),
    ],
)
def test_promote_refuses_anything_but_a_raise_with_a_reason(memory, number, maturity, reason):
    memory.learn("eng-1@rig", "rig-eng-1", "only", at=1)
    with pytest.raises(SeatError):
        memory.promote("eng-1@rig", number, maturity, "rig-eng-1", reason, at=2)
    assert memory.learned("eng-1@rig")[0]["maturity"] == "note"


def test_retire_marks_a_note_and_keeps_every_number(memory):
    memory.learn("eng-1@rig", "rig-eng-1", "first", at=1)
    memory.learn("eng-1@rig", "rig-eng-1", "second", at=2)
    memory.retire("eng-1@rig", 1, "operator", "it quotes the voice keyword", at=5)
    first, second = memory.learned("eng-1@rig")
    assert first["retired"] == {"by": "operator", "reason": "it quotes the voice keyword", "at": 5}
    assert (second["text"], "retired" in second) == ("second", False)


@pytest.mark.parametrize(
    ("number", "reason", "refusal"),
    [
        (2, "no entry 2", "^seat eng-1@rig has no learned note 2$"),
        (0, "x", "^seat eng-1@rig has no learned note 0$"),
        (1, "  ", "^a retirement needs a reason$"),
    ],
)
def test_retire_refuses_a_missing_note_or_an_empty_reason(memory, number, reason, refusal):
    memory.learn("eng-1@rig", "rig-eng-1", "only", at=1)
    with pytest.raises(SeatError, match=refusal):
        memory.retire("eng-1@rig", number, "operator", reason, at=2)
    assert "retired" not in memory.learned("eng-1@rig")[0]


def test_a_retired_note_cannot_be_retired_again(memory):
    memory.learn("eng-1@rig", "rig-eng-1", "only", at=1)
    memory.retire("eng-1@rig", 1, "operator", "stale", at=2)
    with pytest.raises(SeatError, match="^learned note 1 is already retired$"):
        memory.retire("eng-1@rig", 1, "master@a1-1", "again", at=3)
    assert memory.learned("eng-1@rig")[0]["retired"]["by"] == "operator"


def test_a_swarm_culture_is_empty_until_set_and_kept_per_swarm():
    import fakeredis

    culture = SwarmCulture(fakeredis.FakeRedis(decode_responses=True))
    assert culture.get("rig") == ""
    culture.set("rig", "plain words on the page")
    assert (culture.get("rig"), culture.get("other")) == ("plain words on the page", "")
