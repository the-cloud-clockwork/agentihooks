import pytest

from scripts.gates import log as gate_log
from scripts.swarm import review_rounds
from scripts.swarm.store import SwarmError

CLOSED = (
    "Review is closed after three rounds: any finding still open gets one ruling from the other reader, "
    "then the merge proceeds."
)


def test_the_first_two_rounds_carry_no_notice(tmp_path):
    assert review_rounds.start("sw", "t1", tmp_path) == {"task": "t1", "round": 1}
    assert review_rounds.start("sw", "t1", tmp_path) == {"task": "t1", "round": 2}


def test_a_task_at_three_rounds_gets_the_close_notice(tmp_path):
    review_rounds.start("sw", "t1", tmp_path)
    review_rounds.start("sw", "t1", tmp_path)
    assert review_rounds.start("sw", "t1", tmp_path) == {"task": "t1", "round": 3, "notice": CLOSED}


def test_a_fourth_round_is_refused_with_the_close_text(tmp_path):
    for _ in range(3):
        review_rounds.start("sw", "t1", tmp_path)
    with pytest.raises(SwarmError) as refused:
        review_rounds.start("sw", "t1", tmp_path)
    assert str(refused.value) == f"task t1 gets no fourth review round. {CLOSED}"
    with pytest.raises(SwarmError):
        review_rounds.start("sw", "t1", tmp_path)


def test_rounds_count_per_task_and_per_swarm(tmp_path):
    for _ in range(3):
        review_rounds.start("sw", "t1", tmp_path)
    assert review_rounds.start("sw", "t2", tmp_path) == {"task": "t2", "round": 1}
    assert review_rounds.start("other", "t1", tmp_path) == {"task": "t1", "round": 1}


def test_rounds_live_in_the_swarm_home_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(gate_log, "swarm_home", lambda: tmp_path)
    review_rounds.start("sw", "t1")
    assert (tmp_path / "sw" / "gates" / "reviews" / "counts" / "t1" / "rounds").read_text() == "1"


def test_rounds_live_in_the_home_given(tmp_path, monkeypatch):
    monkeypatch.setattr(gate_log, "swarm_home", lambda: tmp_path / "default")
    review_rounds.start("sw", "t1", tmp_path / "given")
    assert (tmp_path / "given" / "sw" / "gates" / "reviews" / "counts" / "t1" / "rounds").read_text() == "1"
    assert not (tmp_path / "default").exists()
