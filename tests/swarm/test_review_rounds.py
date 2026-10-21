import json

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
    assert (tmp_path / "sw" / "gates" / "reviews" / "counts" / "t1" / "rounds-before-pr").read_text() == "1"


def test_rounds_live_in_the_home_given(tmp_path, monkeypatch):
    monkeypatch.setattr(gate_log, "swarm_home", lambda: tmp_path / "default")
    review_rounds.start("sw", "t1", tmp_path / "given")
    assert (tmp_path / "given" / "sw" / "gates" / "reviews" / "counts" / "t1" / "rounds-before-pr").read_text() == "1"
    assert not (tmp_path / "default").exists()


@pytest.mark.parametrize("current", ["first", "second"])
def test_legacy_rounds_remain_bound_to_the_first_pull_request(tmp_path, current):
    first = "https://github.com/o/r/pull/1"
    second = "https://github.com/o/r/pull/2"
    legacy = tmp_path / "sw" / "gates" / "reviews" / "counts" / "t1" / "rounds"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("3")
    if current == "first":
        with pytest.raises(SwarmError, match="no fourth review round"):
            review_rounds.start("sw", "t1", tmp_path, pr_url=first)
    assert review_rounds.start("sw", "t1", tmp_path, pr_url=second, first_pr=first)["round"] == 1
    with pytest.raises(SwarmError, match="no fourth review round"):
        review_rounds.start("sw", "t1", tmp_path, pr_url=first)
    assert review_rounds.start("sw", "t1", tmp_path, pr_url=second)["round"] == 2


def test_rounds_spent_before_any_pull_request_do_not_count_against_the_first_one(tmp_path):
    first = "https://github.com/o/r/pull/1"
    for _ in range(3):
        review_rounds.start("sw", "t1", tmp_path)
    with pytest.raises(SwarmError, match="no fourth review round"):
        review_rounds.start("sw", "t1", tmp_path)
    assert review_rounds.start("sw", "t1", tmp_path, pr_url=first) == {"task": "t1", "round": 1}
    assert review_rounds.start("sw", "t1", tmp_path, pr_url=first, first_pr=first)["round"] == 2
    assert review_rounds.counts("sw", "t1", tmp_path) == {"rounds-before-pr": 3, "rounds": 2}


def test_counts_read_the_given_home_without_combining_pull_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(gate_log, "swarm_home", lambda: tmp_path / "default")
    given = tmp_path / "given"
    first = "https://github.com/o/r/pull/1"
    second = "https://github.com/o/r/pull/2"
    assert review_rounds.counts("sw", "t1", given) == {}
    review_rounds.start("sw", "t1", given, pr_url=first)
    review_rounds.start("sw", "t1", given, pr_url=first)
    review_rounds.start("sw", "t1", given, pr_url=second)
    assert sorted(review_rounds.counts("sw", "t1", given).values()) == [1, 2]
    assert review_rounds.counts("sw", "t1") == {}


def _logged(home):
    path = gate_log.gates_dir("sw", home) / "reviews" / "rounds.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_each_granted_round_is_logged_for_the_metrics(tmp_path):
    review_rounds.start("sw", "t1", tmp_path)
    review_rounds.start("sw", "t1", tmp_path, pr_url="https://github.com/o/r/pull/2")
    logged = _logged(tmp_path)
    assert [(line["task"], line["counter"], line["round"]) for line in logged] == [
        ("t1", "rounds-before-pr", 1),
        ("t1", "rounds", 1),
    ]
    assert all(isinstance(line["at"], int) and line["at"] > 0 for line in logged)


def test_a_refused_round_is_not_logged(tmp_path):
    for _ in range(3):
        review_rounds.start("sw", "t1", tmp_path)
    with pytest.raises(SwarmError):
        review_rounds.start("sw", "t1", tmp_path)
    assert [line["round"] for line in _logged(tmp_path)] == [1, 2, 3]


def test_a_logged_round_carries_the_wall_clock_in_milliseconds(tmp_path, monkeypatch):
    monkeypatch.setattr(review_rounds.time, "time", lambda: 1_800_000_000.25)
    review_rounds.start("sw", "t1", tmp_path)
    assert [line["at"] for line in _logged(tmp_path)] == [1_800_000_000_250]
