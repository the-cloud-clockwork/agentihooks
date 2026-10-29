import pytest

from scripts.swarm import done_gate
from scripts.swarm.ledger_events import PullRequest

pytestmark = pytest.mark.unit

FIRST = "https://github.com/o/r/pull/9"
SECOND = "https://github.com/o/r/pull/10"


def states(**found):
    seen = []

    def read(url):
        seen.append(url)
        state = found.get(url)
        return None if state is None else PullRequest(state, 1 if state == "MERGED" else None, 1, False)

    read.seen = seen
    return read


def by_url(**found):
    return states(**{{"first": FIRST, "second": SECOND}[key]: value for key, value in found.items()})


def split_task(pulls, kind="code"):
    return {"id": "t1", "kind": kind, "split": ["1-2", "3-4"], "split_pulls": pulls}


@pytest.mark.parametrize("kind", ["code", "ci"])
def test_a_split_task_closes_only_when_every_half_has_merged(kind):
    read = by_url(first="MERGED", second="MERGED")
    assert done_gate.refusal(split_task([FIRST, SECOND], kind), SECOND, read) == ""
    assert read.seen == [FIRST, SECOND]


def test_done_is_refused_while_a_half_is_unmerged():
    read = by_url(first="MERGED", second="OPEN")
    assert done_gate.refusal(split_task([FIRST, SECOND]), FIRST, read) == (
        f"half 2 of this split task: pull request {SECOND} is open, not merged; merge it, then run swarm done again"
    )


def test_an_unmerged_first_half_is_named_before_the_second_is_read():
    read = by_url(first="CLOSED", second="MERGED")
    assert done_gate.refusal(split_task([FIRST, SECOND]), SECOND, read) == (
        f"half 1 of this split task: pull request {FIRST} is closed, not merged; merge it, then run swarm done again"
    )
    assert read.seen == [FIRST]


def test_done_is_refused_while_a_half_has_no_pull_request():
    read = by_url(first="MERGED")
    assert done_gate.refusal(split_task([FIRST]), FIRST, read) == (
        "half 2 of this split task has no pull request yet: record it with swarm pr <url>, merge it, "
        "then run swarm done again"
    )
    assert read.seen == []


def test_the_given_pull_request_binds_the_next_unbound_half():
    read = by_url(first="MERGED", second="MERGED")
    assert done_gate.refusal(split_task([FIRST]), SECOND, read) == ""
    assert read.seen == [FIRST, SECOND]


def test_an_unreadable_half_refuses_done():
    read = by_url(first="MERGED")
    assert done_gate.refusal(split_task([FIRST, SECOND]), SECOND, read) == (
        f"half 2 of this split task: could not read pull request {SECOND} from GitHub; "
        "run swarm done again when it answers"
    )


def test_a_split_research_task_is_not_gated():
    read = by_url()
    assert done_gate.refusal(split_task([], "research"), "", read) == ""
    assert read.seen == []


def test_cmd_done_reads_every_half_of_a_split_task(monkeypatch):
    from scripts.swarm import cli

    seen = []
    pulls = {FIRST: PullRequest("MERGED", 1, 1, False), SECOND: PullRequest("OPEN", None, 1, False)}
    monkeypatch.setattr(cli.ledger_events, "view", lambda url: seen.append(url) or pulls[url])
    read = cli._pull_reader(SECOND, pulls[SECOND])
    assert (read(SECOND), read(FIRST)) == (pulls[SECOND], pulls[FIRST])
    assert seen == [FIRST]
