import pytest

from scripts.swarm import cli, idle, merge_queue
from scripts.swarm.ledger_events import PullRequest
from scripts.swarm.store import AgentRecord
from tests.swarm.test_cli import env, run  # noqa: F401

ME = "engineer@a1b2c3-0001"
URL = "https://github.com/o/r/pull/7"


@pytest.fixture
def waiting(env, monkeypatch):  # noqa: F811
    store, ledger, runtime = env
    assert run("sw", "create", "--repo", "/repo") == 0
    store.put_agent("sw", AgentRecord(name=ME, lane="eng", task="t1", seat="eng-1@sw"))
    ledger.rows["t1"].update(state="claimed", claimed_by=ME)
    monkeypatch.setattr(cli.ledger_events, "view", lambda url: PullRequest("OPEN", None, 1, False, queued=True))
    state = {"state": "OPEN", "queued": True, "entry": {"position": 15}}

    def read(action, url):
        assert (action, url) == ("state", URL)
        return state

    monkeypatch.setattr(merge_queue, "operate", read)
    return store, ledger, state


def test_a_long_prerequisite_wait_directs_stacking_or_blocking_without_idling(waiting, capsys):
    store, _, _ = waiting
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 1
    error = capsys.readouterr().err
    assert "stack" in error and "publish" in error and "block" in error
    assert URL in error and "15" in error
    assert "next step" in error and "reopen" in error
    assert idle.wait(store.redis, "sw", ME) is None


@pytest.mark.parametrize("position", [1, 5, None])
def test_a_short_or_unknown_prerequisite_queue_preserves_merge_waits(waiting, position):
    store, _, state = waiting
    state["entry"]["position"] = position
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 0
    assert idle.wait(store.redis, "sw", ME)["on"] == {"kind": "merge", "target": URL}


def test_the_tasks_final_deliverable_can_wait_at_any_queue_position(waiting):
    store, ledger, _ = waiting
    ledger.rows["t1"]["pr_url"] = URL
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 0
    assert idle.wait(store.redis, "sw", ME)["on"]["target"] == URL


def test_block_retains_the_stack_and_tick_reopens_only_after_prerequisite_merge(waiting, env, monkeypatch):  # noqa: F811
    store, ledger, _ = waiting
    note = "Stack published on continuation branch; next step restack after prerequisite merge"
    ledger.rows["t1"].update(branch="continuation", pr_url="https://github.com/o/r/pull/8")
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 1
    assert run("sw", "--as", ME, "block", note) == 0
    assert ledger.rows["t1"]["state"] == "blocked"
    assert ledger.rows["t1"]["branch"] == "continuation"
    assert any(row[:2] == ("t1", note) for row in ledger.comments)
    assert store.agents("sw")[0].state == "finished"
    cli.tick("sw", store, ledger, env[2], 2_000)
    assert ledger.rows["t1"]["state"] == "blocked"
    monkeypatch.setattr(cli.ledger_events, "view", lambda url: PullRequest("MERGED", 2, 1, False))
    cli.tick("sw", store, ledger, env[2], 3_000)
    assert ledger.rows["t1"]["state"] == "open"
    assert ledger.rows["t1"]["claimed_by"] == ""
    assert ledger.rows["t1"]["branch"] == "continuation"
