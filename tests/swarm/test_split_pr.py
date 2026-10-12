import pytest

from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_intent_cli import started  # noqa: F401

pytestmark = pytest.mark.xdist_group("fakeredis")

FIRST = "https://github.com/o/r/pull/9"
SECOND = "https://github.com/o/r/pull/10"


def test_swarm_pr_binds_each_pull_request_of_a_split_task_to_the_next_half(started):  # noqa: F811
    _, ledger, _ = started
    ledger.rows["t1"]["split"] = ["1-2", "3-4"]
    assert run("sw", "pr", FIRST) == 0
    assert ledger.rows["t1"]["split_pulls"] == [FIRST]
    assert run("sw", "pr", FIRST) == 0
    assert ledger.rows["t1"]["split_pulls"] == [FIRST]
    assert run("sw", "pr", SECOND) == 0
    assert ledger.rows["t1"]["split_pulls"] == [FIRST, SECOND]


def test_swarm_pr_past_the_last_half_binds_nothing(started):  # noqa: F811
    _, ledger, _ = started
    ledger.rows["t1"].update(split=["1-2", "3-4"], split_pulls=[FIRST, SECOND])
    assert run("sw", "pr", "https://github.com/o/r/pull/11") == 0
    assert ledger.rows["t1"]["split_pulls"] == [FIRST, SECOND]


def test_swarm_pr_on_an_unsplit_task_records_no_halves(started):  # noqa: F811
    _, ledger, _ = started
    assert run("sw", "pr", FIRST) == 0
    assert "split_pulls" not in ledger.rows["t1"]
