import pytest

from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_intent_cli import started  # noqa: F401

pytestmark = pytest.mark.xdist_group("fakeredis")

FIRST = "https://github.com/o/r/pull/9"
SECOND = "https://github.com/o/r/pull/10"


def test_swarm_pr_binds_each_pull_request_of_a_split_task_to_the_half_it_names(started):  # noqa: F811
    _, ledger, _ = started
    ledger.rows["t1"]["split"] = ["1-2", "3-4"]
    assert run("sw", "pr", FIRST, "--half", "1") == 0
    assert ledger.rows["t1"]["split_pulls"] == [FIRST]
    assert run("sw", "pr", FIRST) == 0
    assert ledger.rows["t1"]["split_pulls"] == [FIRST]
    assert run("sw", "pr", SECOND, "--half", "2") == 0
    assert ledger.rows["t1"]["split_pulls"] == [FIRST, SECOND]


@pytest.mark.parametrize(
    "argv, said",
    [
        ((), "this task is split in 2 halves: name this pull request's half with --half 1"),
        (("--half", "2"), "half 1 of this split task is next: record it before half 2"),
        (("--half", "3"), "this task is split in 2 halves: name this pull request's half with --half 1"),
    ],
)
def test_swarm_pr_on_a_split_task_binds_halves_only_in_order(started, capsys, argv, said):  # noqa: F811
    _, ledger, _ = started
    ledger.rows["t1"]["split"] = ["1-2", "3-4"]
    assert run("sw", "pr", FIRST, *argv) != 0
    assert said in capsys.readouterr().err
    assert "split_pulls" not in ledger.rows["t1"] and not ledger.rows["t1"].get("pr_url")


def test_a_bound_pull_request_names_its_own_half(started, capsys):  # noqa: F811
    _, ledger, _ = started
    ledger.rows["t1"].update(split=["1-2", "3-4"], split_pulls=[FIRST])
    assert run("sw", "pr", FIRST, "--half", "2") != 0
    assert f"{FIRST} is half 1 of this split task" in capsys.readouterr().err
    assert run("sw", "pr", FIRST, "--half", "1") == 0


def test_half_is_refused_on_an_unsplit_task(started, capsys):  # noqa: F811
    assert run("sw", "pr", FIRST, "--half", "1") != 0
    assert capsys.readouterr().err.strip().endswith("this task is not split: drop --half")


def test_swarm_done_reads_each_half_once(started, monkeypatch):  # noqa: F811
    from scripts.swarm import cli
    from scripts.swarm.ledger_events import PullRequest

    _, ledger, _ = started
    ledger.rows["t1"].update(split=["1-2", "3-4"], split_pulls=[FIRST, SECOND], pr_url=SECOND)
    seen = []
    monkeypatch.setattr(cli.ledger_events, "view", lambda url: seen.append(url) or PullRequest("MERGED", 1, 1, False))
    assert run("sw", "done", "--pr", SECOND) == 0
    assert (seen, ledger.rows["t1"]["state"]) == ([SECOND, FIRST], "done")


def test_swarm_pr_past_the_last_half_is_refused(started, capsys):  # noqa: F811
    _, ledger, _ = started
    ledger.rows["t1"].update(split=["1-2", "3-4"], split_pulls=[FIRST, SECOND], pr_url=SECOND)
    assert run("sw", "pr", "https://github.com/o/r/pull/11", "--half", "2") != 0
    assert f"every half of this split task has a pull request: {FIRST}, {SECOND}" in capsys.readouterr().err
    assert (ledger.rows["t1"]["split_pulls"], ledger.rows["t1"]["pr_url"]) == ([FIRST, SECOND], SECOND)


def test_swarm_pr_on_an_unsplit_task_records_no_halves(started):  # noqa: F811
    _, ledger, _ = started
    assert run("sw", "pr", FIRST) == 0
    assert "split_pulls" not in ledger.rows["t1"]
