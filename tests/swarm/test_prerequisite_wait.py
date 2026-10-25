import pytest

from scripts.swarm import cli, idle, merge_queue, prerequisite_wait
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
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL, "--final") == 0
    assert idle.wait(store.redis, "sw", ME)["on"]["target"] == URL


def test_block_retains_the_stack_and_tick_reopens_only_after_prerequisite_merge(waiting, env, monkeypatch):  # noqa: F811
    store, ledger, _ = waiting
    note = f"Stack published on continuation branch; next step restack after prerequisite {URL} merges"
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


@pytest.mark.parametrize("state", ["OPEN", "CLOSED", None])
def test_blocked_continuation_does_not_reopen_without_a_merged_prerequisite(waiting, monkeypatch, state):
    store, ledger, _ = waiting
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 1
    assert run("sw", "--as", ME, "block", f"Stack published; next step restack after {URL}") == 0
    monkeypatch.setattr(cli.ledger_events, "view", lambda url: PullRequest(state, None, 1, False) if state else None)
    assert prerequisite_wait.reopen_pass("sw", store, ledger, ledger.rows) == []
    assert ledger.rows["t1"]["state"] == "blocked"


@pytest.mark.parametrize("change", [{"state": "done"}, {"claimed_by": "someone-else"}, {"claim_generation": 1}])
def test_prerequisite_merge_never_reopens_a_changed_task(waiting, monkeypatch, change):
    store, ledger, _ = waiting
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 1
    assert run("sw", "--as", ME, "block", f"Stack published; next step restack after {URL}") == 0
    ledger.rows["t1"].update(change)
    expected = dict(ledger.rows["t1"])
    monkeypatch.setattr(cli.ledger_events, "view", lambda url: pytest.fail("changed task must not read GitHub"))
    assert prerequisite_wait.reopen_pass("sw", store, ledger, ledger.rows) == []
    assert ledger.rows["t1"] == expected


def test_block_without_a_refused_prerequisite_keeps_its_existing_behavior(waiting, monkeypatch):
    store, ledger, _ = waiting
    assert run("sw", "--as", ME, "block", "Waiting for operator credentials") == 0
    monkeypatch.setattr(cli.ledger_events, "view", lambda url: pytest.fail("ordinary block must not read GitHub"))
    assert prerequisite_wait.reopen_pass("sw", store, ledger, ledger.rows) == []
    assert ledger.rows["t1"]["state"] == "blocked"


def test_prerequisite_block_releases_a_claim_with_an_unfinished_task_dependency(waiting):
    store, ledger, _ = waiting
    ledger.rows["t1"]["depends_on"] = ["t2"]
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 1
    assert run("sw", "--as", ME, "block", f"Stack published; next step restack after {URL}") == 0
    assert ledger.rows["t1"]["state"] == "blocked"
    assert store.agents("sw")[0].state == "finished"


@pytest.mark.parametrize("note", ["Waiting for operator credentials", "Stack after https://github.com/o/r/pull/78"])
def test_an_unrelated_block_after_a_refusal_is_not_reopened(waiting, monkeypatch, note):
    store, ledger, _ = waiting
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 1
    assert run("sw", "--as", ME, "block", note) == 0
    monkeypatch.setattr(cli.ledger_events, "view", lambda url: pytest.fail("unrelated block must not read GitHub"))
    assert prerequisite_wait.reopen_pass("sw", store, ledger, ledger.rows) == []
    assert ledger.rows["t1"]["state"] == "blocked"


def test_an_admitted_wait_clears_the_previous_prerequisite_offer(waiting, monkeypatch):
    store, ledger, state = waiting
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 1
    state["entry"]["position"] = 1
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 0
    assert run("sw", "--as", ME, "block", f"New blocker mentions old stack {URL}; next step credentials") == 0
    monkeypatch.setattr(cli.ledger_events, "view", lambda url: pytest.fail("cleared offer must not read GitHub"))
    assert prerequisite_wait.reopen_pass("sw", store, ledger, ledger.rows) == []
    assert ledger.rows["t1"]["state"] == "blocked"


@pytest.mark.parametrize("position", [6, 39])
def test_every_position_beyond_one_group_requires_continuation(waiting, position):
    _, _, state = waiting
    state["entry"]["position"] = position
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 1


@pytest.mark.parametrize("pull", [None, PullRequest("OPEN", None, 1, False), PullRequest("MERGED", 2, 1, False)])
def test_unqueued_or_unreadable_prerequisites_preserve_wait_admission(waiting, monkeypatch, pull):
    store, _, _ = waiting
    monkeypatch.setattr(cli.ledger_events, "view", lambda url: pull)
    monkeypatch.setattr(merge_queue, "operate", lambda *args: pytest.fail("unqueued pull needs no queue read"))
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 0
    assert idle.wait(store.redis, "sw", ME) is not None


@pytest.mark.parametrize("change", [{"state": "MERGED"}, {"queued": False}, {"entry": None}])
def test_a_fresh_queue_read_controls_admission(waiting, change):
    store, _, state = waiting
    state.update(change)
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 0
    assert idle.wait(store.redis, "sw", ME) is not None


def test_a_failed_queue_read_preserves_wait_admission(waiting, monkeypatch):
    store, _, _ = waiting

    def failed(*args):
        raise cli.SwarmError("GitHub unavailable")

    monkeypatch.setattr(merge_queue, "operate", failed)
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 0
    assert idle.wait(store.redis, "sw", ME) is not None


@pytest.mark.parametrize("change", ["agent", "generation"])
def test_another_claimant_cannot_consume_an_old_prerequisite_offer(waiting, monkeypatch, change):
    store, ledger, _ = waiting
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 1
    if change == "agent":
        store.put_agent("sw", AgentRecord(name=ME, lane="eng", task="t2", seat="eng-1@sw"))
        ledger.rows["t2"].update(state="claimed", claimed_by=ME)
        task_id = "t2"
    else:
        store.redis.set(f"{store.key('sw', 'claim', 't1')}:generation", 1)
        task_id = "t1"
    assert run("sw", "--as", ME, "block", f"Stack published; next step restack after {URL}") == 0
    monkeypatch.setattr(cli.ledger_events, "view", lambda url: pytest.fail("stale offer must not read GitHub"))
    assert prerequisite_wait.reopen_pass("sw", store, ledger, ledger.rows) == []
    assert ledger.rows[task_id]["state"] == "blocked"


def test_an_unrelated_refusal_does_not_bypass_unfinished_dependencies(waiting):
    store, ledger, _ = waiting
    ledger.rows["t1"]["depends_on"] = ["t2"]
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 1
    assert run("sw", "--as", ME, "block", "Waiting for credentials") == 0
    assert ledger.rows["t1"]["state"] == "claimed"
    assert idle.wait(store.redis, "sw", ME)["on"] == {"kind": "task", "target": "t2"}


def test_a_queue_that_grows_long_clears_the_previous_idle_wait(waiting):
    store, _, state = waiting
    state["entry"]["position"] = 1
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 0
    assert idle.wait(store.redis, "sw", ME) is not None
    state["entry"]["position"] = 15
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 1
    assert idle.wait(store.redis, "sw", ME) is None


def test_a_recorded_prerequisite_still_requires_continuation(waiting):
    store, ledger, _ = waiting
    ledger.rows["t1"]["pr_url"] = URL
    ledger.rows["t1"]["description"] = "This pull request is the compatibility prerequisite; implementation follows."
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 1
    assert idle.wait(store.redis, "sw", ME) is None


@pytest.mark.parametrize("options", [[], ["--on", "checks", URL], ["--inbox"], ["--on", "reply", "item"]])
def test_final_confirmation_requires_a_merge_wait(waiting, capsys, options):
    assert run("sw", "--as", ME, "wait", *options, "--final") == 1
    assert "--final requires --on merge" in capsys.readouterr().err


def test_final_confirmation_cannot_name_another_pull_request(waiting, capsys):
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL, "--final") == 1
    assert "--final requires the current task pull request" in capsys.readouterr().err


def test_merge_wait_help_names_final_confirmation_and_existing_target_labels(capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.build_parser().parse_args(["sw", "wait", "--help"])
    assert stopped.value.code == 0
    text = capsys.readouterr().out
    assert "--on KIND TARGET" in text
    assert "--final" in text
