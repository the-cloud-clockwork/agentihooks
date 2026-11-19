import hashlib
import json
import re
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.gates import log as gate_log
from scripts.swarm import review_rounds
from scripts.swarm.store import SwarmError
from tests.swarm.review_ledger import ReviewLedger

CLOSED = (
    "Review is closed after three rounds: any finding still open gets one ruling from the other reader, "
    "then the merge proceeds."
)
ME = "engineer@a1b2c3-0001"
FIRST = "https://github.com/o/r/pull/1"
SECOND = "https://github.com/o/r/pull/2"


@pytest.fixture
def stamps(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    made: list[str] = []

    def uuid4() -> SimpleNamespace:
        made.append(f"s{len(made) + 1}")
        return SimpleNamespace(hex=made[-1])

    monkeypatch.setattr(review_rounds, "uuid", SimpleNamespace(uuid4=uuid4))
    return made


def start(ledger, home, task="t1", slug="sw"):
    return review_rounds.start(slug, task, ledger, ME, home)


def test_the_first_two_rounds_carry_no_notice(tmp_path):
    ledger = ReviewLedger()
    assert start(ledger, tmp_path) == {"task": "t1", "round": 1}
    assert start(ledger, tmp_path) == {"task": "t1", "round": 2}


def test_a_task_at_three_rounds_gets_the_close_notice(tmp_path):
    ledger = ReviewLedger()
    start(ledger, tmp_path)
    start(ledger, tmp_path)
    assert start(ledger, tmp_path) == {"task": "t1", "round": 3, "notice": CLOSED}


def test_a_fourth_round_is_refused_with_the_close_text_and_writes_nothing(tmp_path, stamps):
    ledger = ReviewLedger()
    for _ in range(3):
        start(ledger, tmp_path)
    with pytest.raises(SwarmError) as refused:
        start(ledger, tmp_path)
    assert str(refused.value) == f"task t1 gets no fourth review round. {CLOSED}"
    with pytest.raises(SwarmError):
        start(ledger, tmp_path)
    assert len(ledger.writes) == 3
    assert ledger.rows["t1"]["review_rounds"] == {"rounds-before-pr": ["s1", "s2", "s3"]}


def test_rounds_count_per_task_and_per_swarm(tmp_path):
    ledger = ReviewLedger({"t1": {"id": "t1"}, "t2": {"id": "t2"}})
    for _ in range(3):
        start(ledger, tmp_path)
    assert start(ledger, tmp_path, task="t2") == {"task": "t2", "round": 1}
    assert start(ReviewLedger(slug="other"), tmp_path, slug="other") == {"task": "t1", "round": 1}


@pytest.mark.feature("review-state-on-ledger")
def test_a_successor_on_a_fresh_home_spends_the_rounds_its_predecessor_left_on_the_ledger(tmp_path):
    ledger = ReviewLedger({"t1": {"id": "t1", "review_rounds": {"rounds-before-pr": ["a", "b"]}}})
    assert start(ledger, tmp_path / "successor") == {"task": "t1", "round": 3, "notice": CLOSED}
    with pytest.raises(SwarmError, match="no fourth review round"):
        start(ledger, tmp_path / "another-hive")


@pytest.mark.feature("review-state-on-ledger")
def test_a_round_is_written_on_the_ledger_task_by_the_agent_and_nothing_counts_on_this_machine(tmp_path, stamps):
    ledger = ReviewLedger({"t1": {"id": "t1", "review_rounds": {"rounds-before-pr": ["a"]}, "review_readers": ["x"]}})
    start(ledger, tmp_path)
    assert ledger.writes == [
        {
            "task": "t1",
            "fields": {"review_rounds": {"rounds-before-pr": ["a", "s1"]}},
            "by": ME,
            "if_match": {"review_rounds": {"rounds-before-pr": ["a"]}},
        }
    ]
    assert ledger.rows["t1"]["review_readers"] == ["x"]
    assert sorted(path.name for path in (tmp_path / "sw" / "gates" / "reviews").iterdir()) == ["rounds.jsonl"]


def test_each_round_carries_a_stamp_of_its_own(tmp_path):
    ledger = ReviewLedger()
    start(ledger, tmp_path)
    start(ledger, tmp_path)
    first, second = ledger.rows["t1"]["review_rounds"]["rounds-before-pr"]
    assert first != second
    assert all(re.fullmatch(r"[0-9a-f]{32}", stamp) for stamp in (first, second))


def test_the_counter_is_one_before_any_pull_request_then_one_per_pull_request_url():
    assert review_rounds.counter("") == "rounds-before-pr"
    assert review_rounds.counter(FIRST) == f"rounds-{hashlib.sha256(FIRST.encode()).hexdigest()}"
    assert review_rounds.counter(SECOND) != review_rounds.counter(FIRST)


@pytest.mark.feature("review-state-on-ledger")
def test_rounds_before_a_pull_request_and_on_each_pull_request_count_apart(tmp_path):
    ledger = ReviewLedger()
    for _ in range(3):
        start(ledger, tmp_path)
    with pytest.raises(SwarmError, match="no fourth review round"):
        start(ledger, tmp_path)
    ledger.rows["t1"]["pr_url"] = FIRST
    assert start(ledger, tmp_path) == {"task": "t1", "round": 1}
    ledger.rows["t1"]["pr_url"] = SECOND
    assert start(ledger, tmp_path) == {"task": "t1", "round": 1}
    ledger.rows["t1"]["pr_url"] = FIRST
    assert start(ledger, tmp_path) == {"task": "t1", "round": 2}
    assert review_rounds.counts(ledger.rows["t1"]) == {
        "rounds-before-pr": 3,
        review_rounds.counter(FIRST): 2,
        review_rounds.counter(SECOND): 1,
    }


class Unlisted(ReviewLedger):
    """A ledger that answers no row on the reads it is told to miss, counting each read."""

    def __init__(self, missed: set[int]) -> None:
        super().__init__({"t9": {"id": "t9"}})
        self.missed, self.reads = missed, 0

    def task(self, slug: str, task_id: str) -> dict[str, Any] | None:
        self.reads += 1
        return None if self.reads in self.missed else super().task(slug, task_id)


def test_a_task_the_ledger_does_not_hold_is_refused_before_any_review_write(tmp_path):
    rounds_ledger, readers_ledger = Unlisted(missed={1}), Unlisted(missed={1})
    with pytest.raises(SwarmError) as refused:
        start(rounds_ledger, tmp_path, task="t9")
    with pytest.raises(SwarmError) as unrecorded:
        review_rounds.record_reader("sw", "t9", "spec-reader", readers_ledger, ME)
    assert str(refused.value) == str(unrecorded.value) == "task t9 is not on ledger sw"
    assert (rounds_ledger.reads, rounds_ledger.writes, readers_ledger.writes) == (1, [], [])


def test_a_reread_that_misses_the_task_does_not_count_the_round_twice(tmp_path):
    ledger = Unlisted(missed={2})
    assert start(ledger, tmp_path, task="t9") == {"task": "t9", "round": 1}
    assert (len(ledger.writes), review_rounds.counts(ledger.rows["t9"])) == (2, {"rounds-before-pr": 1})


def test_counts_rounds_and_readers_read_a_task_row_as_copies():
    assert (review_rounds.counts({}), review_rounds.rounds({}), review_rounds.readers({})) == ({}, {}, [])
    empty = {"review_rounds": None, "review_readers": None}
    assert (review_rounds.counts(empty), review_rounds.rounds(empty), review_rounds.readers(empty)) == ({}, {}, [])
    row = {"review_rounds": {"rounds": ["a", "b"]}, "review_readers": ["spec-reader"]}
    counted, held, read = review_rounds.counts(row), review_rounds.rounds(row), review_rounds.readers(row)
    assert (counted, held, read) == ({"rounds": 2}, {"rounds": ["a", "b"]}, ["spec-reader"])
    held["rounds"].append("c")
    read.append("standards-reader")
    assert row == {"review_rounds": {"rounds": ["a", "b"]}, "review_readers": ["spec-reader"]}


@pytest.mark.feature("review-state-on-ledger")
def test_a_reader_is_recorded_once_on_the_ledger_task_under_a_guard_on_the_readers_read():
    ledger = ReviewLedger()
    review_rounds.record_reader("sw", "t1", "spec-reader", ledger, ME)
    review_rounds.record_reader("sw", "t1", "spec-reader", ledger, ME)
    assert ledger.rows["t1"]["review_readers"] == ["spec-reader"]
    assert ledger.writes == [
        {"task": "t1", "fields": {"review_readers": ["spec-reader"]}, "by": ME, "if_match": {"review_readers": []}}
    ]


class Racing(ReviewLedger):
    """A ledger where another writer lands a reader between this writer's read and each of its first writes."""

    def __init__(self, races: int) -> None:
        super().__init__()
        self.races = races

    def update_task(
        self,
        slug: str,
        task_id: str,
        fields: dict[str, Any],
        by: str = "swarm",
        if_state: tuple[str, ...] = (),
        if_match: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if len(self.writes) < self.races:
            row = self.rows[task_id]
            row["review_readers"] = sorted({*row.get("review_readers", []), f"other-{len(self.writes)}"})
        return super().update_task(slug, task_id, fields, by, if_state, if_match)


@pytest.mark.feature("review-state-on-ledger")
def test_two_readers_racing_on_the_same_task_both_land():
    ledger = Racing(races=1)
    review_rounds.record_reader("sw", "t1", "spec-reader", ledger, ME)
    assert ledger.rows["t1"]["review_readers"] == ["other-0", "spec-reader"]
    assert [write["if_match"] for write in ledger.writes] == [
        {"review_readers": []},
        {"review_readers": ["other-0"]},
    ]


def test_a_reader_that_lands_on_the_last_write_is_recorded():
    ledger = Racing(races=review_rounds.WRITES - 1)
    review_rounds.record_reader("sw", "t1", "spec-reader", ledger, ME)
    assert "spec-reader" in ledger.rows["t1"]["review_readers"]
    assert len(ledger.writes) == review_rounds.WRITES == 5


def test_a_reader_the_guard_keeps_refusing_is_refused_after_five_writes():
    ledger = Racing(races=99)
    with pytest.raises(SwarmError) as refused:
        review_rounds.record_reader("sw", "t1", "spec-reader", ledger, ME)
    assert str(refused.value) == "reader spec-reader not recorded on task t1: its review readers changed under 5 writes"
    assert "spec-reader" not in ledger.rows["t1"]["review_readers"]
    assert len(ledger.writes) == 5


class RacingRounds(ReviewLedger):
    """A ledger where another round lands on the named counter between this writer's read and each of its first
    writes."""

    def __init__(self, races: int, name: str = "rounds-before-pr", rows: dict[str, dict[str, Any]] | None = None):
        super().__init__(rows)
        self.races, self.name = races, name

    def update_task(
        self,
        slug: str,
        task_id: str,
        fields: dict[str, Any],
        by: str = "swarm",
        if_state: tuple[str, ...] = (),
        if_match: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if len(self.writes) < self.races:
            held = self.rows[task_id].setdefault("review_rounds", {})
            held[self.name] = [*held.get(self.name, []), f"other-{len(self.writes)}"]
        return super().update_task(slug, task_id, fields, by, if_state, if_match)


@pytest.mark.feature("review-state-on-ledger")
def test_a_round_racing_another_on_the_same_counter_counts_after_it(tmp_path, stamps):
    ledger = RacingRounds(races=1)
    assert start(ledger, tmp_path) == {"task": "t1", "round": 2}
    assert ledger.rows["t1"]["review_rounds"] == {"rounds-before-pr": ["other-0", "s1"]}
    assert [write["if_match"] for write in ledger.writes] == [
        {"review_rounds": {}},
        {"review_rounds": {"rounds-before-pr": ["other-0"]}},
    ]


@pytest.mark.feature("review-state-on-ledger")
def test_a_round_that_loses_the_third_round_to_a_race_is_refused(tmp_path):
    ledger = RacingRounds(races=1, rows={"t1": {"id": "t1", "review_rounds": {"rounds-before-pr": ["a", "b"]}}})
    with pytest.raises(SwarmError, match="no fourth review round"):
        start(ledger, tmp_path)
    assert (ledger.rows["t1"]["review_rounds"], len(ledger.writes)) == ({"rounds-before-pr": ["a", "b", "other-0"]}, 1)


def test_a_round_that_lands_on_the_last_write_counts(tmp_path):
    ledger = RacingRounds(races=review_rounds.WRITES - 1, name="rounds-other")
    assert start(ledger, tmp_path) == {"task": "t1", "round": 1}
    assert (len(ledger.writes), review_rounds.counts(ledger.rows["t1"])) == (
        5,
        {"rounds-other": 4, "rounds-before-pr": 1},
    )


def test_a_round_the_guard_keeps_refusing_is_refused_after_five_writes(tmp_path):
    ledger = RacingRounds(races=99, name="rounds-other")
    with pytest.raises(SwarmError) as refused:
        start(ledger, tmp_path)
    assert str(refused.value) == "task t1 spent no review round: its review rounds changed under each of 5 writes"
    assert (len(ledger.writes), review_rounds.counts(ledger.rows["t1"])) == (5, {"rounds-other": 5})
    assert not (tmp_path / "sw" / "gates" / "reviews" / "rounds.jsonl").exists()


def _logged(home):
    path = gate_log.gates_dir("sw", home) / "reviews" / "rounds.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_each_granted_round_is_logged_for_the_metrics(tmp_path):
    ledger = ReviewLedger()
    start(ledger, tmp_path)
    ledger.rows["t1"]["pr_url"] = SECOND
    start(ledger, tmp_path)
    logged = _logged(tmp_path)
    assert [(line["task"], line["counter"], line["round"]) for line in logged] == [
        ("t1", "rounds-before-pr", 1),
        ("t1", review_rounds.counter(SECOND), 1),
    ]
    assert all(isinstance(line["at"], int) and line["at"] > 0 for line in logged)


def test_the_metrics_log_lives_in_the_swarm_home_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(gate_log, "swarm_home", lambda: tmp_path)
    review_rounds.start("sw", "t1", ReviewLedger(), ME)
    assert [line["round"] for line in _logged(tmp_path)] == [1]


def test_a_refused_round_is_not_logged(tmp_path):
    ledger = ReviewLedger()
    for _ in range(3):
        start(ledger, tmp_path)
    with pytest.raises(SwarmError):
        start(ledger, tmp_path)
    assert [line["round"] for line in _logged(tmp_path)] == [1, 2, 3]


def test_a_logged_round_carries_the_wall_clock_in_milliseconds(tmp_path, monkeypatch):
    monkeypatch.setattr(review_rounds.time, "time", lambda: 1_800_000_000.25)
    start(ReviewLedger(), tmp_path)
    assert [line["at"] for line in _logged(tmp_path)] == [1_800_000_000_250]


@pytest.mark.feature("review-round-metrics-from-ledger")
def test_a_round_stamp_carries_the_wall_clock_in_milliseconds_ahead_of_its_unique_part(tmp_path, monkeypatch, stamps):
    monkeypatch.setattr(review_rounds, "time", SimpleNamespace(time=lambda: 1_800_000_000.25))
    ledger = ReviewLedger()
    start(ledger, tmp_path)
    assert ledger.rows["t1"]["review_rounds"] == {"rounds-before-pr": ["1800000000250-s1"]}


@pytest.mark.feature("review-round-metrics-from-ledger")
def test_a_stamp_carries_a_time_only_when_it_opens_with_digits_and_a_dash():
    stamped = ("1800000000250-s1", "0-x", "12-", "a1b2", "12ab-c", "-c", "12", "")
    assert [review_rounds.stamped_at(stamp) for stamp in stamped] == [1_800_000_000_250, 0, 12, *[None] * 5]
