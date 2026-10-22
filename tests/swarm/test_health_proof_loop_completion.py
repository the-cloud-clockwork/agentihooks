import json

import pytest

from scripts.swarm.health.findings import Limits, proof_loops
from scripts.swarm.health.verdicts import VerdictStore


HOUR = 3_600_000
SUBJECTS = ("t1294", "t1391", "t1394")


def starts(subject, count):
    return [{"kind": "task started", "target": f"tasks/{subject}", "at": at} for at in range(count)]


def loops(tasks, count=4, reviews=None):
    events = [event for subject in tasks for event in starts(subject, count)]
    return proof_loops(events, tasks, Limits(), reviews or {})


@pytest.fixture
def board():
    from fakeredis import FakeRedis

    return VerdictStore(FakeRedis(decode_responses=True), "findings")


@pytest.mark.parametrize("count", [4, 7])
def test_resolved_done_subjects_stay_closed_on_repeated_later_passes(board, count):
    tasks = {subject: {"id": subject, "state": "pr"} for subject in SUBJECTS}
    assert len(board.visible(loops(tasks), 0, HOUR)) == 3
    for subject in tasks:
        board.judge(f"proof-loop/{subject}", "resolved", "task completed", "master", 0)
        tasks[subject]["state"] = "done"
    before = board.redis.hgetall(board.key)
    for at in (HOUR, HOUR + 1, 2 * HOUR, 24 * HOUR):
        assert board.visible(loops(tasks, count), at, HOUR) == []
    assert board.redis.hgetall(board.key) == before


def test_completed_task_has_no_new_proof_loop_for_old_reruns_or_reviews():
    tasks = {"t1": {"id": "t1", "state": "done"}}
    assert loops(tasks) == []
    assert loops(tasks, count=1, reviews={"t1": {"head": 4}}) == []


def test_reopened_task_returns_only_after_new_loop_evidence(board):
    tasks = {"t1": {"id": "t1", "state": "pr"}}
    board.visible(loops(tasks), 0, HOUR)
    board.judge("proof-loop/t1", "resolved", "task completed", "master", 0)
    tasks["t1"]["state"] = "done"
    assert board.visible(loops(tasks), HOUR, HOUR) == []
    tasks["t1"]["state"] = "claimed"
    assert board.visible(loops(tasks), HOUR + 1, HOUR) == []
    [returned] = board.visible(loops(tasks, count=5), HOUR + 2, HOUR)
    assert returned["id"] == "proof-loop/t1"
    assert returned["verdict"]["value"] == "resolved"
    assert returned["evidence"][1] == "started 5 times (4 reruns)"
    record = json.loads(board.redis.hget(board.key, "proof-loop/t1"))
    assert record["verdict"]["measure"] == 3
    assert record["measure"] == 4


@pytest.mark.parametrize("state", ["open", "claimed", "pr", "blocked"])
def test_active_tasks_still_cross_the_rerun_and_review_thresholds(state):
    tasks = {"t1": {"id": "t1", "state": state}}
    assert loops(tasks, count=3, reviews={"t1": {"head": 3}}) == []
    [reruns] = loops(tasks)
    assert reruns.measure == 3
    [reviews] = loops(tasks, count=1, reviews={"t1": {"head": 4}})
    assert reviews.measure == 4
